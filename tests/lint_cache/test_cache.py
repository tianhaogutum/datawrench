import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, MARKER, MAGIC
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.engine import lint
from datawrench.lint_cache.fingerprint import InputError, digest


@pytest.mark.parametrize("damage", ["partial", "checksum", "schema", "shape", "null", "symlink"])
def test_corruption_recomputes(project, damage):
    first = lint(["bad.sql"])
    path = next((project / ".sqlfluff_cache").glob("*.json"))
    data = json.loads(path.read_text())
    if damage == "partial":
        path.write_text('{"schema":')
    elif damage == "checksum":
        data["record"]["violations"][0]["description"] = "wrong"
        path.write_text(json.dumps(data))
    elif damage == "schema":
        data["schema"] = -1
        path.write_text(json.dumps(data))
    elif damage == "shape":
        data["record"]["violations"] = "invalid"
        path.write_text(json.dumps(data))
    elif damage == "null":
        path.write_text("null")
    else:
        target = project / "untouched"
        target.write_text("sensitive")
        path.unlink()
        path.symlink_to(target)
    notices = []
    assert Cache(project / ".sqlfluff_cache").status()["corrupt"] == 1
    second = lint(["bad.sql"], warn=notices.append)
    assert second.stats.misses == 1
    assert second.records[0]["violations"] == first.records[0]["violations"]
    assert any("Corrupt cache entry" in n for n in notices)
    assert lint(["bad.sql"]).stats.hits == 1
    if damage == "symlink":
        assert target.read_text() == "sensitive"


def test_cache_management(project, capsys):
    lint(["clean.sql", "bad.sql"])
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["entries"] == 2
    (project / "bad.sql").unlink()
    assert main(["gc"]) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == 1
    cache_dir = project / ".sqlfluff_cache"
    unrelated = cache_dir / "keep.txt"
    unrelated.write_text("not our data")
    assert main(["clear"]) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == 1
    assert unrelated.read_text() == "not our data"
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["entries"] == 0


def test_gc_expired_and_corrupt(project):
    lint(["clean.sql", "bad.sql", "parse.sql"])
    entries = sorted((project / ".sqlfluff_cache").glob("*.json"))
    os.utime(entries[0], (time.time() - 40 * 86400,) * 2)
    entries[1].write_text("{")
    cache = Cache(project / ".sqlfluff_cache")
    assert cache.gc(30) == 2
    assert cache.status()["entries"] == 1


@pytest.mark.parametrize("path", ["linked", "nested/link/cache"])
def test_cache_symlink_directory_cannot_write_outside(project, path):
    outside = project / "outside"
    outside.mkdir()
    if path == "linked":
        (project / "linked").symlink_to(outside, target_is_directory=True)
    else:
        (project / "nested").mkdir()
        (project / "nested/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InputError, match="symlink"):
        lint(["clean.sql"], cache_dir=path)
    assert not list(outside.iterdir())


def test_maintenance_refuses_unowned_directory(project, capsys):
    destination = project / "unowned"
    destination.mkdir()
    data = destination / ("a" * 64 + ".json")
    data.write_text("unrelated file")
    assert main(["clear", "--cache-dir", str(destination)]) == 2
    assert "unmarked" in capsys.readouterr().err
    assert data.read_text() == "unrelated file"


def test_marker_symlink_and_invalid_marker(project):
    destination = project / "cache"
    destination.mkdir()
    target = project / "marker-source"
    target.write_text(MAGIC)
    (destination / MARKER).symlink_to(target)
    with pytest.raises(InputError, match="symlink"):
        Cache(destination).prepare(create=True)
    (destination / MARKER).unlink()
    (destination / MARKER).write_text("not us")
    with pytest.raises(InputError, match="Unrecognized"):
        Cache(destination).clear()


def test_missing_maintenance_does_not_create_directory(project):
    cache = Cache(project / "missing")
    assert cache.status() == {"entries": 0, "bytes": 0, "corrupt": 0, "histories": 0,
                              "history_bytes": 0, "history_corrupt": 0, "managed_total_bytes": 0}
    assert cache.clear() == cache.gc() == 0
    assert not cache.directory.exists()


@pytest.mark.parametrize("age", ["-1", "nan", "inf"])
def test_invalid_gc_age(project, capsys, age):
    assert main(["gc", "--max-age-days", age]) == 2
    assert "finite and nonnegative" in capsys.readouterr().err


def test_atomic_concurrent_writes_and_interrupted_temporary_file(project):
    # Small threads exercise replacement races without spawning lint workers.
    cache = Cache(project / "atomic")
    cache.prepare(create=True)
    key = digest("same-input")
    source = project / "clean.sql"
    record = {"violations": [], "statistics": {"source_chars": 10}, "timings": {}}
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda i: cache.put(key, source, record), range(12)))
    assert cache.get(key, source) == record
    assert not list(cache.directory.glob(".entry-*"))
    (cache.directory / ".entry-interrupted").write_text("{")
    assert cache.status()["entries"] == 1
    assert cache.get(key, source) == record


def test_concurrent_cold_cache_preparation(project):
    destination = project / "new/cache"
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda i: Cache(destination).prepare(create=True), range(8)))
    assert all(results)
    assert (destination / MARKER).read_text() == MAGIC


def test_fifo_marker_rejected_without_reading(project):
    destination = project / "fifo-cache"
    destination.mkdir()
    os.mkfifo(destination / MARKER)
    with pytest.raises(InputError, match="Unrecognized"):
        Cache(destination).prepare()


def test_over_nested_json_recovery(project):
    lint(["bad.sql"])
    entry = next((project / ".sqlfluff_cache").glob("*.json"))
    entry.write_text("[" * 2000 + "0" + "]" * 2000)
    notices = []
    assert lint(["bad.sql"], warn=notices.append).stats.misses == 1
    assert any("Corrupt" in n for n in notices)


def test_cache_api_rejects_path_traversal_keys(project):
    cache = Cache(project / "bounded")
    cache.prepare(create=True)
    record = {"violations": [], "statistics": {}, "timings": {}}
    for key in ("../escape", "/absolute", "", "x" * 64):
        with pytest.raises(InputError, match="SHA-256"):
            cache.put(key, project / "clean.sql", record)
        with pytest.raises(InputError, match="SHA-256"):
            cache.get(key, project / "clean.sql")
    assert not (project / "escape.json").exists()


def test_oversize_result_is_returned_without_caching(project, monkeypatch):
    from datawrench.lint_cache import cache as cache_module
    monkeypatch.setattr(cache_module, "MAX_ENTRY", 20)
    notices = []
    result = lint(["bad.sql"], warn=notices.append)
    assert result.records[0]["violations"]
    assert result.exit_code == 1
    assert any("not cached" in message for message in notices)
    assert not list((project / ".sqlfluff_cache").glob("*.json"))


def test_maintenance_never_recursively_removes_entry_directories(project):
    cache = Cache(project / "bounded")
    cache.prepare(create=True)
    nested = cache.directory / ("a" * 64 + ".json")
    nested.mkdir()
    (nested / "keep").write_text("unrelated")
    with pytest.raises(InputError, match="subdirectory"):
        cache.clear()
    with pytest.raises(InputError, match="subdirectory"):
        cache.gc()
    assert (nested / "keep").read_text() == "unrelated"
