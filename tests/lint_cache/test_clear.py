"""Clear preflights all managed names before unlinking, without reading data."""
import hashlib
import json
import os
import stat
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, MARKER
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.fingerprint import InputError
from .test_gc_budget import seed


def snapshot(root):
    """Include directory metadata and contents; never traverse symlinks or FIFOs."""
    result = {}

    def visit(path):
        info = path.lstat()
        value = [info.st_ino, info.st_mtime_ns, info.st_mode, info.st_size]
        if stat.S_ISLNK(info.st_mode):
            value.append(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            value.append(hashlib.sha256(path.read_bytes()).hexdigest())
        result[str(path.relative_to(root))] = value
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)

    visit(root)
    return result


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("interface", ["api", "cli"])
def test_clear_preflights_all_managed_directories(project, capsys, kind, interface):
    cache = Cache(project / "clear")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    name = "f" * 64 + ".json" if kind == "entry" else ".inputs-" + "f" * 64
    nested = cache.directory / name
    nested.mkdir()
    (nested / "keep").write_text("untouched")
    (cache.directory / "unknown").write_text("also untouched")
    before = snapshot(cache.directory)

    if interface == "api":
        with pytest.raises(InputError, match="subdirectory"):
            cache.clear()
    else:
        assert main(["clear", "--cache-dir", str(cache.directory)]) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and "subdirectory" in captured.err
    assert snapshot(cache.directory) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
def test_clear_inventory_error_preserves_all_managed_files(project, capsys, monkeypatch, kind):
    cache = Cache(project / "clear")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    name = "f" * 64 + ".json" if kind == "entry" else ".inputs-" + "f" * 64
    denied_path = cache.directory / name
    denied_path.write_text("unreadable inventory")
    before = snapshot(cache.directory)
    original = Path.lstat

    def denied(path):
        if path == denied_path:
            raise PermissionError("synthetic inventory denial")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", denied)
        assert main(["clear", "--cache-dir", str(cache.directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "inventory denial" in captured.err
    assert all(path.exists() for path in pair)
    assert snapshot(cache.directory) == before


def test_clear_unlinks_owned_names_only_and_preserves_result_count(project, capsys, monkeypatch):
    cache = Cache(project / "clear")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    outside = project / "outside"
    outside.mkdir()
    (outside / "private").write_text("never read or remove me")
    outside_before = snapshot(outside)
    for prefix, suffix in (("", ".json"), (".inputs-", "")):
        (cache.directory / (prefix + "b" * 64 + suffix)).write_text("null")
        for token, target in (("c", outside / "private"), ("d", outside),
                              ("e", project / "absent")):
            (cache.directory / (prefix + token * 64 + suffix)).symlink_to(target)
        os.mkfifo(cache.directory / (prefix + "f" * 64 + suffix))
    for name in ("unknown.json", ".entry-interrupted", "A" * 64 + ".json",
                 ".inputs-short", ".inputs-" + "a" * 64 + ".json"):
        (cache.directory / name).write_text("unmanaged")
    (cache.directory / "unknown-link").symlink_to(outside, target_is_directory=True)
    nested = cache.directory / "ordinary-directory"
    nested.mkdir()
    (nested / ("a" * 64 + ".json")).write_text("unmanaged nested result name")
    managed = {*cache.entries(), *cache.histories()}
    before = snapshot(cache.directory)

    def no_read(*args):
        raise AssertionError("clear must not read managed results or histories")

    monkeypatch.setattr(Cache, "_read", no_read)
    monkeypatch.setattr(Cache, "_read_history", no_read)
    assert main(["clear", "--cache-dir", str(cache.directory)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"removed": 6}
    assert all(not p.exists() and not p.is_symlink() for p in managed)
    after = snapshot(cache.directory)
    after.pop(".")  # Removing owned children legitimately changes the cache directory.
    assert after == {name: value for name, value in before.items()
                     if name not in {p.name for p in managed} | {"."}}
    assert snapshot(outside) == outside_before
    assert not (project / "absent").exists()
    assert (cache.directory / MARKER).is_file()
    assert cache.clear() == 0


@pytest.mark.parametrize("state", ["missing", "empty", "marked"])
def test_clear_empty_cache_does_not_create_or_rewrite(project, capsys, state):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    before = snapshot(project)
    assert cache.clear() == 0
    assert main(["clear", "--cache-dir", str(cache.directory)]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == {"removed": 0}
    assert snapshot(project) == before


def test_clear_unlink_failure_is_not_transactional_or_reported_as_success(project, capsys, monkeypatch):
    cache = Cache(project / "clear")
    first, history = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    later = cache.directory / ("b" * 64 + ".json")
    later.write_text("{")
    original = Cache._remove

    def denied(self, path):
        if path == later:
            raise PermissionError("synthetic unlink denial")
        return original(self, path)

    monkeypatch.setattr(Cache, "_remove", denied)
    assert main(["clear", "--cache-dir", str(cache.directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "unlink denial" in captured.err
    assert not first.exists()
    assert later.read_text() == "{" and history.exists()
