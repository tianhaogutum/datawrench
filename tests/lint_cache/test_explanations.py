import json

import pytest
from sqlfluff.core import Linter

from datawrench.lint_cache import engine
from datawrench.lint_cache.cache import Cache
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.engine import lint
from datawrench.lint_cache.fingerprint import digest


def decisions(result):
    return {item["filepath"]: (item["action"], item["reasons"])
            for item in result.explanations}


def test_consecutive_content_settings_and_corruption(project, monkeypatch):
    paths = ["clean.sql", "bad.sql"]
    assert decisions(lint(paths)) == {p: ("linted", ["not_cached"]) for p in paths}
    original = Linter.render_string
    calls = []

    def render(self, raw, fname, *args, **kwargs):
        calls.append(fname)
        return original(self, raw, fname, *args, **kwargs)

    monkeypatch.setattr(Linter, "render_string", render)
    assert decisions(lint(paths)) == {p: ("replayed", ["cache_hit"]) for p in paths}
    assert calls == []
    (project / "clean.sql").write_text("SELECT 2 AS value;\n")
    assert decisions(lint(paths)) == {
        "clean.sql": ("linted", ["content_changed"]),
        "bad.sql": ("replayed", ["cache_hit"]),
    }
    assert calls == ["clean.sql"]
    config = project / ".sqlfluff"
    config.write_text(config.read_text() + "\nrules = LT01\n")
    assert decisions(lint(paths)) == {p: ("linted", ["settings_changed"]) for p in paths}
    # Damage the exact current key, rather than a superseded entry.
    current = next(p for p in (project / ".sqlfluff_cache").glob(".inputs-*")
                   if json.loads(p.read_text())["source"] == str(project / "clean.sql"))
    key = json.loads(current.read_text())["key"]
    (current.parent / (key + ".json")).write_text('{"schema":')
    assert decisions(lint(paths)) == {
        "clean.sql": ("linted", ["corrupt_entry"]),
        "bad.sql": ("replayed", ["cache_hit"]),
    }
    assert decisions(lint(paths)) == {p: ("replayed", ["cache_hit"]) for p in paths}


def test_version_dependency_and_multiple_reasons(project, monkeypatch):
    lint(["clean.sql"])
    stamp = engine.engine_stamp()
    upgraded = {**stamp, "sqlfluff": "test-version",
                "packages": [(name, "test-version" if name.lower() == "sqlfluff" else version)
                             for name, version in stamp["packages"]],
                "plugins": [(name, value, dist, "test-version" if dist.lower() == "sqlfluff" else version)
                            for name, value, dist, version in stamp["plugins"]]}
    monkeypatch.setattr(engine, "engine_stamp", lambda: upgraded)
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("linted", ["sqlfluff_version_changed"])
    monkeypatch.setattr(engine, "engine_stamp", lambda: {
        **upgraded, "plugins": [*upgraded["plugins"], ["test-plugin", "pkg:plugin", "pkg", "2"]]})
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("linted", ["dependencies_changed"])
    (project / "clean.sql").write_text("SELECT 2;\n")
    (project / ".sqlfluff").write_text("[sqlfluff]\ndialect = ansi\ntemplater = raw\nrules = LT01\n")
    assert decisions(lint(["clean.sql"]))["clean.sql"] == (
        "linted", ["content_changed", "settings_changed"])


def test_template_dependency_separate_from_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".sqlfluff"
    config.write_text("[sqlfluff]\ndialect = ansi\ntemplater = jinja\n"
                      "[sqlfluff:templater:jinja]\nloader_search_path = templates\n"
                      "[sqlfluff:templater:jinja:context]\nvalue = 1\n")
    (tmp_path / "templates").mkdir()
    dependency = tmp_path / "templates/part.j2"
    dependency.write_text("SELECT {{ value }};\n")
    (tmp_path / "query.sql").write_text("{% include 'part.j2' %}")
    lint(["query.sql"])
    config.write_text(config.read_text().replace("value = 1", "value = 2"))
    assert decisions(lint(["query.sql"]))["query.sql"] == ("linted", ["settings_changed"])
    dependency.write_text("SELECT {{ value }} AS changed;\n")
    assert decisions(lint(["query.sql"]))["query.sql"] == ("linted", ["dependencies_changed"])


def test_explanation_json_and_human_preserve_diagnostics(project, capsys, monkeypatch):
    sql_secret = "SQL_CONTENT_SENTINEL_043"
    env_secret = "ENV_SECRET_SENTINEL_043"
    monkeypatch.setenv("PRIVATE_TOKEN_043", env_secret)
    (project / "bad.sql").write_text(f"SELECT  1; -- {sql_secret}\n")
    assert main(["lint", "bad.sql", "--format", "cache-json"]) == 1
    cold = json.loads(capsys.readouterr().out)
    assert cold["files"] == [{"filepath": "bad.sql", "action": "linted", "reasons": ["not_cached"]}]
    assert cold["stats"]["misses"] == 1
    assert main(["lint", "bad.sql", "--format", "cache-json"]) == 1
    warm = json.loads(capsys.readouterr().out)
    assert warm["files"][0] == {"filepath": "bad.sql", "action": "replayed", "reasons": ["cache_hit"]}
    assert warm["stats"]["hits"] == 1
    assert main(["lint", "bad.sql", "--format", "json", "--explain"]) == 1
    output = capsys.readouterr()
    assert isinstance(json.loads(output.out), list)  # upstream JSON format stays intact
    assert 'replayed "bad.sql": cache_hit' in output.err
    assert "linted" not in output.err
    safe_output = json.dumps([cold, warm]) + output.err
    for entry in (project / ".sqlfluff_cache").glob(".inputs-*"):
        safe_output += entry.read_text()
    assert sql_secret not in safe_output
    assert env_secret not in safe_output
    assert "PRIVATE_TOKEN_043" not in safe_output


def test_missing_history_missing_entry_and_rollback(project):
    lint(["clean.sql"])
    original = (project / "clean.sql").read_text()
    (project / "clean.sql").write_text("SELECT 2;\n")
    lint(["clean.sql"])
    (project / "clean.sql").write_text(original)
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("replayed", ["cache_hit"])
    history = next((project / ".sqlfluff_cache").glob(".inputs-*"))
    current = json.loads(history.read_text())
    (history.parent / (current["key"] + ".json")).unlink()
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("linted", ["entry_missing"])
    history.unlink()
    # Existing caches without explanation metadata still replay and seed history.
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("replayed", ["cache_hit"])
    assert history.exists()
    (project / "clean.sql").write_text("SELECT 3;\n")
    history.write_text("{")
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("linted", ["history_unavailable"])


@pytest.mark.parametrize("damage", ["checksum", "components", "oversize", "nested", "fifo"])
def test_damaged_history_cannot_control_cache_reuse(project, damage):
    lint(["clean.sql"])
    history = next((project / ".sqlfluff_cache").glob(".inputs-*"))
    entry = json.loads(history.read_text())
    if damage == "checksum":
        entry["components"]["content"] = "0" * 64
        history.write_text(json.dumps(entry))
    elif damage == "components":
        del entry["components"]["content"]
        del entry["checksum"]
        history.write_text(json.dumps({**entry, "checksum": digest(entry)}))
    elif damage == "oversize":
        history.write_text(" " * (16 * 1024 + 1))
    elif damage == "nested":
        history.write_text("[" * 2000 + "0" + "]" * 2000)
    else:
        import os
        history.unlink()
        os.mkfifo(history)
    # Bad history never turns an otherwise valid exact key into a miss.
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("replayed", ["cache_hit"])
    assert history.is_file()
    assert lint(["clean.sql"]).stats.hits == 1


def test_trusted_library_environment_explanation_is_secret_free(project, monkeypatch, capsys):
    library = project / "explain_library"
    library.mkdir()
    (library / "__init__.py").write_text("def answer(): return 1\n")
    (project / ".sqlfluff").write_text(
        "[sqlfluff]\ndialect = ansi\ntemplater = jinja\n"
        "[sqlfluff:templater:jinja]\nlibrary_path = explain_library\n"
        "[sqlfluff:templater:jinja:context]\nsecret = CONFIG_SECRET_043\n")
    (project / "query.sql").write_text("SELECT {{ answer() }}; -- SQL_SECRET_043\n")
    lint(["query.sql"], trust_template_code=True)
    lint(["query.sql"], trust_template_code=True)  # account for generated bytecode
    assert lint(["query.sql"], trust_template_code=True).stats.hits == 1
    monkeypatch.setenv("PRIVATE_TOKEN_043", "ENV_SECRET_043")
    assert main(["lint", "query.sql", "--trust-template-code", "--format", "cache-json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["files"][0]["reasons"] == ["dependencies_changed"]
    output = captured.out + captured.err
    output += "".join(p.read_text() for p in (project / ".sqlfluff_cache").glob(".inputs-*"))
    for secret in ("CONFIG_SECRET_043", "SQL_SECRET_043", "ENV_SECRET_043", "PRIVATE_TOKEN_043"):
        assert secret not in output


def test_history_write_failure_does_not_break_legacy_replay(project, monkeypatch):
    lint(["clean.sql"])
    next((project / ".sqlfluff_cache").glob(".inputs-*")).unlink()

    def read_only(*args):
        raise PermissionError("synthetic read-only cache")

    monkeypatch.setattr(Cache, "_write", read_only)
    notices = []
    result = lint(["clean.sql"], warn=notices.append)
    assert decisions(result)["clean.sql"] == ("replayed", ["cache_hit"])
    assert any("Could not save input history" in notice for notice in notices)


def test_child_settings_explanation_is_per_file(project):
    child = project / "child"
    child.mkdir()
    (child / "query.sql").write_text("SELECT 1;\n")
    lint(["clean.sql", "child"])
    (child / ".sqlfluff").write_text("[sqlfluff]\nrules = LT01\n")
    assert decisions(lint(["clean.sql", "child"])) == {
        "clean.sql": ("replayed", ["cache_hit"]),
        "child/query.sql": ("linted", ["settings_changed"]),
    }


def test_bypassed_and_skipped_are_explicit(project):
    result = lint(["clean.sql"], no_cache=True, cache_dir="unusable")
    assert decisions(result)["clean.sql"] == ("linted", ["cache_disabled"])
    assert not (project / "unusable").exists()
    (project / "large.sql").write_text("SELECT 1;\n" * 3000)
    assert decisions(lint(["large.sql"]))["large.sql"] == ("skipped", ["large_file_skipped"])
    (project / ".sqlfluff").write_text("[sqlfluff]\ndialect = ansi\ntemplater = jinja\n")
    (project / "random.sql").write_text("SELECT {{ [1] | random }};\n")
    assert decisions(lint(["random.sql"]))["random.sql"] == ("linted", ["template_cache_bypassed"])


@pytest.mark.parametrize("operation", ["clear", "gc"])
def test_history_maintenance_and_symlink_boundary(project, operation):
    lint(["clean.sql"])
    history = next((project / ".sqlfluff_cache").glob(".inputs-*"))
    target = project / "keep"
    target.write_text("untouched")
    history.unlink()
    history.symlink_to(target)
    (project / "clean.sql").write_text("SELECT 2;\n")
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("linted", ["history_unavailable"])
    assert target.read_text() == "untouched"
    assert not history.is_symlink()
    cache = Cache(project / ".sqlfluff_cache")
    if operation == "clear":
        assert cache.clear() == 2
    else:
        assert cache.gc(0) == 2
    assert not list(cache.directory.glob(".inputs-*"))
    assert decisions(lint(["clean.sql"]))["clean.sql"] == ("linted", ["not_cached"])
