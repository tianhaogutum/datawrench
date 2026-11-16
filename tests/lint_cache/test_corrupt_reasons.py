"""Fixed opt-in corruption codes share the existing bounded validation pass."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache import cache as cache_module
from datawrench.lint_cache.cache import Cache, MAX_ENTRY
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.fingerprint import InputError
from .test_clear import snapshot
from .test_gc_budget import seed
from .test_status import ZERO


FLAGS = ["--details", "--corrupt-reasons"]


def test_reason_categories_precedence_single_pass_and_compatibility(project, capsys, monkeypatch):
    cache = Cache(project / "reasons")
    valid = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    orphan = seed(cache, project / "bad.sql", "b" * 64,
                  time.time_ns() - 40 * 86400 * 10**9)
    orphan[0].unlink()
    (project / "bad.sql").unlink()
    expected = {path.name: None for path in (*valid, orphan[1])}
    denied = set()
    no_json = set()
    outside = project / "private-source-path"
    outside.write_text("PRIVATE SQL, config, and exception contents")
    for prefix, suffix, limit in (("", ".json", MAX_ENTRY), (".inputs-", "", 16 * 1024)):
        for token, text, reason in (("c", "{PRIVATE", "invalid_data"),
                                     ("d", "{}", "unreadable")):
            path = cache.directory / (prefix + token * 64 + suffix)
            path.write_text(text)
            expected[path.name] = reason
            if reason == "unreadable":
                denied.add(path)
        path = cache.directory / (prefix + "e" * 64 + suffix)
        with path.open("wb") as handle:
            handle.truncate(limit + 1)
        expected[path.name] = "oversized"
        denied.add(path)  # Size takes precedence over open failure.
        no_json.add(path)
        for token, target in (("0", outside), ("1", project / "absent"), ("2", project)):
            path = cache.directory / (prefix + token * 64 + suffix)
            path.symlink_to(target)
            expected[path.name] = "non_regular"
            no_json.add(path)
        path = cache.directory / (prefix + "f" * 64 + suffix)
        os.mkfifo(path)
        expected[path.name] = "non_regular"
        no_json.add(path)
    (cache.directory / "unknown").write_text("unmanaged")
    before = snapshot(project)
    original_open, original_load = os.open, json.load
    calls, loads = [], []

    def checked_open(path, flags, *args, **kwargs):
        candidate = Path(path)
        if candidate.name in expected:
            calls.append(candidate.name)
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
        if candidate in denied:
            raise PermissionError("PRIVATE validator error with /private/source.sql")
        return original_open(path, flags, *args, **kwargs)

    def checked_load(handle):
        # fdopen names are descriptors: inspect /proc only in this Linux test.
        path = Path(os.readlink(f"/proc/self/fd/{handle.fileno()}"))
        assert path not in no_json
        loads.append(path.name)
        return original_load(handle)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", checked_open)
        patch.setattr(json, "load", checked_load)
        result = cache.status(details=True, corrupt_reasons=True)
        assert sorted(calls) == sorted(expected)  # One validator open per name.
        assert sorted(loads) == sorted({*expected} - {p.name for p in denied | no_json})
        plain = cache.status(details=True)
        assert [{k: v for k, v in item.items() if k != "corrupt_reason"}
                for item in result["details"]] == plain["details"]
        assert {k: v for k, v in result.items() if k != "details"} == cache.status()
        assert all(set(item) == {"name", "kind", "bytes", "corrupt", "corrupt_reason"}
                   for item in result["details"])
        assert {item["name"]: item["corrupt_reason"] for item in result["details"]} == expected
        assert all(item["corrupt"] == (item["corrupt_reason"] is not None)
                   for item in result["details"])
        assert [item["name"] for item in result["details"]] == sorted(expected)
        assert sum(item["bytes"] for item in result["details"]) == result["managed_total_bytes"]
        command = ["status", "--cache-dir", str(cache.directory), *FLAGS]
        assert main(command) == 0
        first = capsys.readouterr()
        assert first.err == "" and json.loads(first.out) == result
        assert "PRIVATE" not in first.out and str(project) not in first.out
        assert main(command) == 0
        assert capsys.readouterr() == first
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("damage", ["checksum", "shape", "schema", "null", "encoding", "nested"])
def test_invalid_data_does_not_disclose_validator_errors(project, capsys, kind, damage):
    cache = Cache(project / "invalid")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    path = pair[kind == "history"]
    data = json.loads(path.read_text())
    if damage == "checksum":
        data["checksum"] = "PRIVATE checksum"
    elif damage == "schema":
        data["schema"] = "PRIVATE schema"
    elif damage == "shape":
        data["record" if kind == "entry" else "components"] = "PRIVATE shape"
    if damage in ("checksum", "schema", "shape"):
        path.write_text(json.dumps(data))
    elif damage == "null":
        path.write_text("null")
    elif damage == "encoding":
        path.write_bytes(b'\xffPRIVATE')
    else:
        path.write_text("[" * 2000 + "0" + "]" * 2000)
    before = snapshot(project)
    assert main(["status", "--cache-dir", str(cache.directory), *FLAGS]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "PRIVATE" not in captured.out and str(project) not in captured.out
    result = json.loads(captured.out)
    assert {item["name"]: item["corrupt_reason"] for item in result["details"]} == {
        p.name: "invalid_data" if p == path else None for p in pair}
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
def test_limit_equality_valid_and_one_byte_over_oversized(project, kind):
    cache = Cache(project / "limits")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    path = pair[kind == "history"]
    limit = MAX_ENTRY if kind == "entry" else 16 * 1024
    with path.open("ab") as handle:
        handle.write(b" " * (limit - path.stat().st_size))
    assert all(item["corrupt_reason"] is None
               for item in cache.status(details=True, corrupt_reasons=True)["details"])
    with path.open("ab") as handle:
        handle.write(b" ")
    reasons = {item["name"]: item["corrupt_reason"]
               for item in cache.status(details=True, corrupt_reasons=True)["details"]}
    assert reasons[path.name] == "oversized"


@pytest.mark.parametrize("kind", ["entry", "history"])
def test_sensitive_validator_exception_is_fixed_invalid_data(project, capsys, monkeypatch, kind):
    cache = Cache(project / "sensitive")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    before = snapshot(project)

    def invalid(*args):
        raise ValueError("PRIVATE validator path /secrets/query.sql and SQL")

    monkeypatch.setattr(Cache, "_read" if kind == "entry" else "_read_history", invalid)
    assert main(["status", "--cache-dir", str(cache.directory), *FLAGS]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "PRIVATE" not in captured.out and "/secrets" not in captured.out
    assert {item["name"]: item["corrupt_reason"] for item in json.loads(captured.out)["details"]} == {
        p.name: "invalid_data" if p == pair[kind == "history"] else None for p in pair}
    assert snapshot(project) == before


@pytest.mark.parametrize("state", ["missing", "empty", "marked"])
def test_absent_and_empty_reason_details_remain_read_only(project, capsys, state):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    before = snapshot(project)
    assert cache.status(details=True, corrupt_reasons=True) == {**ZERO, "details": []}
    assert main(["status", "--cache-dir", str(cache.directory), *FLAGS]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == {**ZERO, "details": []}
    assert snapshot(project) == before


def test_reasons_require_details_before_inspection(project, capsys, monkeypatch):
    cache = Cache(project / "absent")
    before = snapshot(project)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid option combination must fail before cache inspection")

    monkeypatch.setattr(Cache, "prepare", unexpected)
    with pytest.raises(InputError, match="requires --details"):
        cache.status(corrupt_reasons=True)
    assert main(["status", "--cache-dir", str(cache.directory), "--corrupt-reasons"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "requires --details" in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "lstat", "enumeration"])
def test_reasons_keep_inventory_refusals_separate(project, capsys, monkeypatch, kind, failure):
    cache = Cache(project / "refusals")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
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
        assert main(["status", "--cache-dir", str(cache.directory), *FLAGS]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before
