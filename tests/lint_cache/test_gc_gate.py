"""Retention gate changes only CLI exits, using the existing read-only GC plan."""
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
from .test_gc_summary import mixed


MODES = [[], ["--summary"], ["--summary", "--reason-bytes"]]
OPTIONS = ["--fail-on-planned", "--dry-run", "--summary", "--reason-bytes"]
COMBINATIONS = list(itertools.product((False, True), repeat=4))


def flags(options):
    return [name for name, enabled in zip(OPTIONS, options) if enabled]


@pytest.mark.parametrize("state", ["expired", "source_missing", "over_budget",
                                    "history_expired", "entry_missing", "history_source_missing"])
def test_valid_retention_work_triggers_gate(project, capsys, state):
    cache = Cache(project / "retention")
    entry, history = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    options = []
    if state == "expired":
        os.utime(entry, ns=(1, 1))
    elif state == "history_expired":
        os.utime(history, ns=(1, 1))
    elif state == "over_budget":
        options = ["--max-bytes", "0"]
    elif state in ("entry_missing", "history_source_missing"):
        entry.unlink()
    if state in ("source_missing", "history_source_missing"):
        (project / "clean.sql").unlink()
    before = snapshot(project)
    # Structural gate deliberately accepts these same valid stale items.
    assert main(["status", "--cache-dir", str(cache.directory), "--fail-on-corrupt"]) == 0
    structural = json.loads(capsys.readouterr().out)
    assert structural["corrupt"] == structural["history_corrupt"] == 0
    for mode in MODES:
        command = ["gc", "--cache-dir", str(cache.directory), "--dry-run", *options, *mode]
        assert main(command) == 0
        plain = capsys.readouterr()
        assert main([*command, "--fail-on-planned"]) == 1
        assert capsys.readouterr() == plain and plain.err == ""
        report = json.loads(plain.out)
        assert report["planned_histories"] == 1
        assert report["planned_entries"] == int(state in ("expired", "source_missing", "over_budget"))
        if mode:
            assert str(project) not in plain.out and entry.name not in plain.out and history.name not in plain.out
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("damage", ["json", "fifo", "file_link", "absent_link", "dir_link",
                                    "oversized", "unreadable"])
def test_corrupt_and_zero_byte_plans_trigger_gate(project, capsys, monkeypatch, kind, damage):
    cache = Cache(project / "cache")
    cache.prepare(create=True)
    path = cache.directory / ("a" * 64 + ".json" if kind == "entry" else ".inputs-" + "a" * 64)
    target = project / "PRIVATE-target"
    target.write_text("PRIVATE SQL")
    if damage == "fifo":
        os.mkfifo(path)
    elif damage.endswith("link"):
        path.symlink_to({"file_link": target, "absent_link": project / "missing",
                         "dir_link": project}[damage])
    elif damage == "oversized":
        with path.open("wb") as handle:
            handle.truncate((MAX_ENTRY if kind == "entry" else MAX_HISTORY) + 1)
    else:
        path.write_text("{PRIVATE")
    before = snapshot(project)
    original = os.open

    def checked_open(candidate, *args, **kwargs):
        assert Path(candidate) != target
        if Path(candidate) == path and damage == "unreadable":
            raise PermissionError("PRIVATE raw exception")
        return original(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", checked_open)
        for mode in MODES:
            command = ["gc", "--cache-dir", str(cache.directory), "--dry-run", *mode]
            assert main(command) == 0
            plain = capsys.readouterr()
            assert main([*command, "--fail-on-planned"]) == 1
            assert capsys.readouterr() == plain and plain.err == ""
            report = json.loads(plain.out)
            assert (report["planned_entries"], report["planned_histories"]) == ((1, 0) if kind == "entry" else (0, 1))
            if damage == "fifo":
                assert report["bytes_before"] == report["bytes_after"] == 0
            if mode:
                assert path.name not in plain.out and str(project) not in plain.out
            assert "PRIVATE" not in plain.out
    assert snapshot(project) == before


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "live"])
def test_no_work_exits_zero_without_creation(project, capsys, state):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    elif state == "live":
        seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    before = snapshot(project)
    for mode in MODES:
        command = ["gc", "--cache-dir", str(cache.directory), "--dry-run", *mode]
        assert main(command) == 0
        plain = capsys.readouterr()
        assert main([*command, "--fail-on-planned"]) == 0
        assert capsys.readouterr() == plain and plain.err == ""
        report = json.loads(plain.out)
        assert report["planned_entries"] == report["planned_histories"] == 0
    assert snapshot(project) == before


@pytest.mark.parametrize("mode", MODES)
def test_identical_single_bounded_pass_and_idle_snapshots(project, mixed, monkeypatch, capsys, mode):
    cache, budget, expected, keep = mixed
    before = snapshot(project)
    trace = []

    def tracked(original, name):
        def call(*args, **kwargs):
            trace.append((name, tuple(str(a.directory) if isinstance(a, Cache) else str(a)
                                      for a in args), str(kwargs)))
            return original(*args, **kwargs)
        return call

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not write or remove files")

    with monkeypatch.context() as patch:
        for owner, name in ((Path, "stat"), (Path, "lstat"), (os, "fstat"), (os, "open"),
                            (Cache, "collect"), (Cache, "_inventory"), (Cache, "_read"),
                            (Cache, "_read_history"), (json, "load")):
            patch.setattr(owner, name, tracked(getattr(owner, name), name))
        patch.setattr(Cache, "_write", forbidden)
        patch.setattr(Cache, "_remove", forbidden)
        command = ["gc", "--dry-run", "--cache-dir", str(cache.directory), "--max-bytes", str(budget), *mode]
        observed, traces = [], []
        for gate in (False, True, True):
            trace.clear()
            assert main([*command, *(["--fail-on-planned"] if gate else [])]) == int(gate)
            observed.append(capsys.readouterr())
            traces.append(trace[:])
            assert sum(event[0] == "collect" for event in trace) == 1
            assert sum(event[0] == "_inventory" for event in trace) == 1
            reads = [event[1][1] for event in trace if event[0] in ("_read", "_read_history")]
            assert sorted(reads) == sorted(str(p) for p in expected | keep)
        assert observed[0] == observed[1] == observed[2] and observed[0].err == ""
        assert traces[0] == traces[1] == traces[2]  # No extra opens/stats/loads/target traversal.
    report = json.loads(observed[0].out)
    assert (report["planned_entries"], report["planned_histories"]) == (6, 8)
    assert report["removed"] == report["histories_removed"] == 0
    if mode:
        assert all(p.name not in observed[0].out for p in expected | keep)
        assert all(word not in observed[0].out for word in (str(project), "private", "sensitive"))
    assert snapshot(project) == before


@pytest.mark.parametrize("options", COMBINATIONS)
def test_all_option_combinations_before_preparation_and_default_apply(project, capsys, monkeypatch, options):
    gate, dry_run, summary, byte_counts = options
    invalid = (gate and not dry_run) or (byte_counts and not summary)
    cache = Cache(project / "combinations")
    cache.prepare(create=True)
    fifo = cache.directory / ("a" * 64 + ".json")
    os.mkfifo(fifo)
    before = snapshot(project)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid options must fail before preparation")
    with monkeypatch.context() as patch:
        if invalid:
            patch.setattr(Cache, "prepare", forbidden)
        assert main(["gc", "--cache-dir", str(cache.directory), *flags(options)]) == (2 if invalid else int(gate))
    captured = capsys.readouterr()
    if invalid:
        error = "--fail-on-planned requires --dry-run" if gate and not dry_run else "--reason-bytes requires --summary"
        assert captured.out == "" and error in captured.err
        assert snapshot(project) == before
    else:
        report = json.loads(captured.out)
        assert captured.err == ""
        assert report["dry_run"] == dry_run and report["planned_entries"] == 1
        assert report["removed"] == int(not dry_run)
        assert ("plan" not in report) == summary
        assert ("planned_reason_bytes" in report) == byte_counts
        if dry_run:
            assert snapshot(project) == before
        else:
            assert not fifo.exists() and cache.clear() == 0


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "lstat", "enumeration"])
@pytest.mark.parametrize("mode", MODES)
def test_inventory_failures_stay_exit_two(project, capsys, monkeypatch, kind, failure, mode):
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
        assert main(["gc", "--cache-dir", str(cache.directory), "--dry-run", "--fail-on-planned", *mode]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("unsafe", ["unmarked", "cache_link", "marker_link", "bad_marker"])
@pytest.mark.parametrize("mode", MODES)
def test_preparation_failures_stay_exit_two(project, capsys, unsafe, mode):
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
    assert main(["gc", "--cache-dir", str(cache.directory), "--dry-run", "--fail-on-planned", *mode]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("options", [["--max-age-days", "-1"], ["--max-age-days", "nan"],
                                     ["--max-age-days", "inf"], ["--max-bytes", "-1"],
                                     ["--max-bytes", "1.5"]])
def test_invalid_limits_before_prepare(project, monkeypatch, capsys, options):
    before = snapshot(project)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid limit reached preparation")
    monkeypatch.setattr(Cache, "prepare", forbidden)
    try:
        result = main(["gc", "--dry-run", "--fail-on-planned", *options])
    except SystemExit as exc:
        result = exc.code
    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == "" and options[0] in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("command", ["lint", "status", "clear"])
def test_gate_is_gc_only_before_action(project, monkeypatch, capsys, command):
    before = snapshot(project)
    def forbidden(*args, **kwargs):
        pytest.fail("parser must reject flag before any action")
    monkeypatch.setattr(Cache, "prepare", forbidden)
    with pytest.raises(SystemExit) as exc:
        main([command, "--fail-on-planned"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "unrecognized arguments: --fail-on-planned" in captured.err
    assert snapshot(project) == before
