"""CI gate changes only status exits, reusing the successful snapshot unchanged."""
import itertools
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, MARKER, MAX_ENTRY, MAX_HISTORY
from datawrench.lint_cache.cli import main
from .test_clear import snapshot
from .test_gc_budget import seed


OPTIONS = ["--details", "--corrupt-reasons", "--corrupt-counts", "--corrupt-bytes"]
VALID = [(details, reasons, counts, byte_counts)
         for details, reasons, counts, byte_counts in itertools.product((False, True), repeat=4)
         if (not reasons or details) and (not byte_counts or counts)]
INVALID = [options for options in itertools.product((False, True), repeat=4)
           if options not in VALID]


def flags(options):
    return [name for name, enabled in zip(OPTIONS, options) if enabled]


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("damage", ["json", "fifo", "file_link", "absent_link", "dir_link",
                                    "oversized", "unreadable"])
def test_gate_each_corrupt_kind_preserves_default_json(project, capsys, monkeypatch, kind, damage):
    cache = Cache(project / "cache")
    cache.prepare(create=True)
    path = cache.directory / ("a" * 64 + ".json" if kind == "entry" else ".inputs-" + "a" * 64)
    target = project / "PRIVATE-target.sql"
    target.write_text("PRIVATE SQL and error text")
    if damage == "fifo":
        os.mkfifo(path)
        assert path.lstat().st_size == 0
    elif damage.endswith("link"):
        path.symlink_to({"file_link": target, "absent_link": project / "missing",
                         "dir_link": project}[damage])
    elif damage == "oversized":
        with path.open("wb") as handle:
            handle.truncate((MAX_ENTRY if kind == "entry" else MAX_HISTORY) + 1)
    else:
        path.write_text("{PRIVATE")
    before = snapshot(project)
    original_open = os.open

    def checked_open(candidate, *args, **kwargs):
        assert Path(candidate) != target
        if Path(candidate) == path and damage == "unreadable":
            raise PermissionError("PRIVATE raw exception /secrets/query.sql")
        return original_open(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", checked_open)
        command = ["status", "--cache-dir", str(cache.directory)]
        assert main(command) == 0
        plain = capsys.readouterr()
        result = json.loads(plain.out)
        assert (result["corrupt"], result["history_corrupt"]) == ((1, 0) if kind == "entry" else (0, 1))
        assert main([*command, "--fail-on-corrupt"]) == 1
        assert capsys.readouterr() == plain
    assert plain.err == ""
    assert "PRIVATE" not in plain.out and str(project) not in plain.out and path.name not in plain.out
    assert snapshot(project) == before


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "valid", "expired", "orphan"])
def test_gate_healthy_and_absent_states_are_zero(project, capsys, state):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    elif state in ("valid", "expired", "orphan"):
        pair = seed(cache, project / "clean.sql", "a" * 64,
                    time.time_ns() if state == "valid" else 1)
        if state == "orphan":
            pair[0].unlink()
            (project / "clean.sql").unlink()
    before = snapshot(project)
    command = ["status", "--cache-dir", str(cache.directory), *OPTIONS]
    assert main(command) == 0
    plain = capsys.readouterr()
    assert main([*command, "--fail-on-corrupt"]) == 0
    assert capsys.readouterr() == plain and plain.err == ""
    result = json.loads(plain.out)
    assert result["corrupt"] == result["history_corrupt"] == 0
    if state == "orphan":
        assert result["histories"] == 1 and result["entries"] == 0
    assert snapshot(project) == before


@pytest.mark.parametrize("options", VALID)
def test_all_valid_combinations_use_one_identical_bounded_pass(project, capsys, monkeypatch, options):
    cache = Cache(project / "mixed")
    pair = seed(cache, project / "clean.sql", "a" * 64, 1)
    broken = cache.directory / ("b" * 64 + ".json")
    broken.write_text("{PRIVATE")
    fifo = cache.directory / (".inputs-" + "c" * 64)
    os.mkfifo(fifo)
    target = project / "PRIVATE-target"
    target.write_text("PRIVATE SQL")
    link = cache.directory / ("d" * 64 + ".json")
    link.symlink_to(target)
    for name in ("unknown.json", ".entry-partial"):
        (cache.directory / name).write_text("PRIVATE unrelated")
    nested = cache.directory / "ordinary"
    nested.mkdir()
    (nested / ("e" * 64 + ".json")).write_text("PRIVATE nested")
    managed = {*pair, broken, fifo, link}
    before = snapshot(project)
    originals = {name: getattr(Cache, name) for name in ("status", "_inventory", "_read", "_read_history")}
    original_open, original_load = os.open, json.load
    original_lstat, original_stat = Path.lstat, Path.stat
    observed = []
    calls = {}

    def wrap(name):
        def record(self, *args, **kwargs):
            calls.setdefault(name, []).append(args[0] if args else None)
            return originals[name](self, *args, **kwargs)
        return record

    def checked_open(candidate, *args, **kwargs):
        candidate = Path(candidate)
        assert candidate != target
        if candidate in managed:
            calls.setdefault("open", []).append(candidate)
            assert args[0] & os.O_NOFOLLOW and args[0] & os.O_NONBLOCK
        return original_open(candidate, *args, **kwargs)

    def checked_load(handle):
        candidate = Path(os.readlink(f"/proc/self/fd/{handle.fileno()}"))
        if candidate in managed:
            calls.setdefault("load", []).append(candidate)
        return original_load(handle)

    def checked_lstat(candidate):
        if candidate in managed:
            calls.setdefault("lstat", []).append(candidate)
        return original_lstat(candidate)

    def checked_stat(candidate, *args, **kwargs):
        assert candidate != target
        if candidate in managed:
            assert kwargs.get("follow_symlinks") is False
            calls.setdefault("stat", []).append(candidate)
        return original_stat(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        for name in originals:
            patch.setattr(Cache, name, wrap(name))
        patch.setattr(os, "open", checked_open)
        patch.setattr(json, "load", checked_load)
        patch.setattr(Path, "lstat", checked_lstat)
        patch.setattr(Path, "stat", checked_stat)
        command = ["status", "--cache-dir", str(cache.directory), *flags(options)]
        for gate in (False, True, True):
            calls = {}
            assert main([*command, *(["--fail-on-corrupt"] if gate else [])]) == int(gate)
            observed.append(capsys.readouterr())
            assert len(calls["status"]) == len(calls["_inventory"]) == 1
            assert set(calls["_read"] + calls["_read_history"]) == managed
            for operation in ("open", "lstat", "stat"):
                assert sorted(calls[operation]) == sorted(managed)
            assert sorted(calls["load"]) == sorted([*pair, broken])
        assert observed[0] == observed[1] == observed[2] and observed[0].err == ""
    output = observed[0].out
    result = json.loads(output)
    assert result["corrupt"] == 2 and result["history_corrupt"] == 1
    assert "PRIVATE" not in output and str(project) not in output
    if not options[0]:
        assert all(path.name not in output for path in managed)
        assert "details" not in result
    assert snapshot(project) == before


@pytest.mark.parametrize("options", INVALID)
def test_gate_keeps_option_dependencies_before_preparation(project, capsys, monkeypatch, options):
    before = snapshot(project)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid dependencies must fail before preparation")

    monkeypatch.setattr(Cache, "prepare", unexpected)
    assert main(["status", "--cache-dir", "absent", "--fail-on-corrupt", *flags(options)]) == 2
    captured = capsys.readouterr()
    expected = "--corrupt-reasons requires --details" if options[1] and not options[0] else "--corrupt-bytes requires --corrupt-counts"
    assert captured.out == "" and expected in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "lstat", "enumeration"])
def test_gate_keeps_inventory_errors_fatal(project, capsys, monkeypatch, kind, failure):
    cache = Cache(project / "refusals")
    seed(cache, project / "clean.sql", "a" * 64, 1)
    path = cache.directory / ("f" * 64 + ".json" if kind == "entry" else ".inputs-" + "f" * 64)
    if failure == "directory":
        path.mkdir()
        (path / "keep").write_text("untouched")
    else:
        path.write_text("{")
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
        assert main(["status", "--cache-dir", str(cache.directory), "--fail-on-corrupt", *OPTIONS]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("unsafe", ["unmarked", "cache_link", "marker_link", "bad_marker"])
def test_gate_keeps_preparation_errors_fatal(project, capsys, unsafe):
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
    elif unsafe == "bad_marker":
        (cache.directory / MARKER).write_text("invalid")
    before = snapshot(project)
    assert main(["status", "--cache-dir", str(cache.directory), "--fail-on-corrupt"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("command", ["lint", "gc", "clear"])
def test_gate_is_status_only_before_any_action(project, capsys, monkeypatch, command):
    before = snapshot(project)

    def unexpected(*args, **kwargs):
        pytest.fail("parser must reject the flag before any action")

    monkeypatch.setattr(Cache, "prepare", unexpected)
    with pytest.raises(SystemExit) as exc:
        main([command, "--fail-on-corrupt"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "unrecognized arguments: --fail-on-corrupt" in captured.err
    assert snapshot(project) == before
