import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlfluff.core import FluffConfig, Linter
from sqlfluff.core.config import clear_config_caches

from datawrench.lint_cache import engine
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.engine import lint
from .conftest import FIXTURES


def stable(records):
    return [{k: v for k, v in r.items() if k != "timings"} for r in records]


def direct(paths, **overrides):
    clear_config_caches()
    config = FluffConfig.from_path(str(Path.cwd()), overrides=overrides or None,
                                  require_dialect=False)
    return Linter(config=config).lint_paths(tuple(paths), processes=1).as_records()


@pytest.mark.parametrize("dialect", ["ansi", "postgres"])
def test_equivalence_real_fixtures(tmp_path, monkeypatch, dialect):
    root = tmp_path / "sql"
    shutil.copytree(FIXTURES / dialect, root)
    monkeypatch.chdir(root)
    expected = direct(["."])
    cold, warm = lint(["."]), lint(["."])
    assert stable(cold.records) == stable(expected)
    assert warm.records == cold.records  # cold timings are intentionally replayed
    assert cold.stats.misses == warm.stats.hits == len(expected)
    assert warm.stats.misses == 0
    assert cold.exit_code == warm.exit_code
    if dialect == "ansi":
        assert any(v["code"] == "PRS" for r in warm.records for v in r["violations"])
        assert all(not r["filepath"].endswith("ignored.sql") for r in warm.records)
        assert next(r for r in warm.records if r["filepath"].endswith("noqa.sql"))["violations"] == []


def test_hit_never_runs_linter(project, monkeypatch):
    cold = lint(["clean.sql"])
    def forbidden(*a, **kw):
        pytest.fail("cache hit invoked renderer")
    monkeypatch.setattr(Linter, "render_string", forbidden)
    warm = lint(["clean.sql"])
    assert warm.stats.hits == 1
    assert warm.records == cold.records


def test_content_config_version_invalidation(project, monkeypatch):
    paths = ["clean.sql", "bad.sql"]
    lint(paths)
    assert lint(paths).stats.hits == 2
    (project / "clean.sql").write_text("SELECT 2 AS value;\n")
    changed = lint(paths)
    assert (changed.stats.hits, changed.stats.misses) == (1, 1)
    (project / ".sqlfluff").write_text("[sqlfluff]\ndialect = ansi\ntemplater = raw\nrules = LT01\n")
    assert lint(paths).stats.misses == 2
    stamp = engine.engine_stamp()
    monkeypatch.setattr(engine, "engine_stamp", lambda: {**stamp, "sqlfluff": "future-version"})
    assert lint(paths).stats.misses == 2
    assert lint(paths).stats.hits == 2
    monkeypatch.setattr(engine, "engine_stamp", lambda: {**stamp, "plugins": [["new-plugin", "2.0"]]})
    assert lint(paths).stats.misses == 2


def test_child_config_only_invalidates_affected(project):
    child = project / "nested"
    child.mkdir()
    (child / "other.sql").write_text("SELECT 1 AS value;\n")
    paths = ["clean.sql", "nested"]
    lint(paths)
    (child / ".sqlfluff").write_text("[sqlfluff]\nrules = LT01\n")
    result = lint(paths)
    assert (result.stats.hits, result.stats.misses) == (1, 1)
    assert stable(result.records) == stable(direct(paths))


def test_override_and_inline_config(project):
    (project / "inline.sql").write_text("-- sqlfluff:rules:LT01\nSELECT  1")
    result = lint(["inline.sql"], dialect="duckdb")
    assert stable(result.records) == stable(direct(["inline.sql"], dialect="duckdb"))
    assert [v["code"] for v in result.records[0]["violations"]] == ["LT01"]


def test_no_cache_does_not_touch_cache(project, monkeypatch):
    # Unusable cache path must not matter when --no-cache is used.
    (project / "blocked").write_text("not a directory")
    a = lint(["bad.sql"], no_cache=True, cache_dir="blocked")
    assert (a.stats.hits, a.stats.misses, a.stats.bypassed) == (0, 1, 1)
    assert not (project / ".sqlfluff_cache").exists()
    cold = lint(["bad.sql"])
    assert stable(a.records) == stable(cold.records)


@pytest.mark.parametrize("file,exit_code", [("clean.sql", 0), ("bad.sql", 1), ("parse.sql", 1)])
def test_exit_codes(project, file, exit_code):
    assert lint([file]).exit_code == exit_code
    assert lint([file]).exit_code == exit_code


def test_warnings_do_not_fail(project, capsys):
    (project / ".sqlfluff").write_text("[sqlfluff]\ndialect = ansi\ntemplater = raw\nwarnings = LT01\nrules = LT01\n")
    for _ in range(2):
        result = lint(["bad.sql"])
        assert result.records[0]["violations"]
        assert all(v["warning"] for v in result.records[0]["violations"])
        assert result.exit_code == 0
    assert main(["lint", "bad.sql", "--format", "github-annotation"]) == 0
    annotations = json.loads(capsys.readouterr().out)
    assert all(a["annotation_level"] == "notice" for a in annotations)


def test_missing_path_and_bad_config(project, capsys):
    assert main(["lint", "missing.sql"]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert main(["lint", "--config", "missing.ini", "clean.sql"]) == 2
    assert not (project / ".sqlfluff_cache").exists()


def test_json_matches_sqlfluff_cli(project, capsys):
    expected_run = subprocess.run(["sqlfluff", "lint", ".", "--format", "json", "--processes", "1"],
                                  capture_output=True, text=True)
    assert expected_run.returncode == 1
    expected = json.loads(expected_run.stdout)
    for _ in range(2):
        assert main(["lint", ".", "--format", "json", "--stats"]) == 1
        captured = capsys.readouterr()
        actual = json.loads(captured.out)
        assert stable(actual) == stable(expected)
        assert set(actual[0]) == set(expected[0])
        assert "files=4" in captured.err


def test_annotation_matches_sqlfluff_cli(project, capsys):
    run = subprocess.run(["sqlfluff", "lint", "bad.sql", "--format", "github-annotation"],
                         capture_output=True, text=True)
    assert run.returncode == 1
    assert main(["lint", "bad.sql", "--format", "github-annotation"]) == 1
    assert json.loads(capsys.readouterr().out) == json.loads(run.stdout)


def test_large_file_skip_reported(project):
    (project / "large.sql").write_text("SELECT 1;\n" * 3000)
    notices = []
    result = lint(["large.sql"], warn=notices.append)
    assert not result.records
    assert result.stats.skipped == 1
    assert any("Skipping" in message for message in notices)
    assert not (project / ".sqlfluff_cache").exists()


def test_no_paths_means_cwd_and_overlapping_paths_deduped(project):
    assert stable(lint().records) == stable(direct([str(Path.cwd())]))
    assert len(lint(["clean.sql", "clean.sql"]).records) == 1


def _run_cli(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_unknown_dialect_option_is_clean_operation_error(project, capsys):
    code, out, err = _run_cli(capsys, "lint", "--dialect", "nosuch", ".")
    assert code == 2
    assert out == ""
    assert "Unknown dialect" in err and "ansi" in err and "postgres" in err
    assert "Traceback" not in err


def test_unknown_dialect_in_config_is_clean_operation_error(project, capsys):
    (project / ".sqlfluff").write_text("[sqlfluff]\ndialect = nosuch\n")
    code, out, err = _run_cli(capsys, "lint", ".")
    assert code == 2
    assert out == ""
    assert "Unknown dialect" in err and "ansi" in err
    assert "Traceback" not in err


def test_unknown_dialect_in_nested_config_is_clean_operation_error(project, capsys):
    sub = project / "nested"
    sub.mkdir()
    (sub / "q.sql").write_text("select 1 as a\n")
    (sub / ".sqlfluff").write_text("[sqlfluff]\ndialect = nosuch\n")
    code, out, err = _run_cli(capsys, "lint", "nested")
    assert code == 2
    assert out == ""
    assert "Unknown dialect" in err
