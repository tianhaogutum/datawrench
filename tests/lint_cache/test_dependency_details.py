"""Contracts for opt-in, hash-only dependency attribution."""
import json
import sys

import pytest
from sqlfluff.core import FluffConfig, Linter

from datawrench.lint_cache import engine
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.engine import lint
from datawrench.lint_cache.fingerprint import Fingerprinter, digest
from .test_engine import direct, stable


@pytest.fixture
def included(project):
    (project / ".sqlfluff").write_text(
        "[sqlfluff]\ndialect = ansi\ntemplater = jinja\n"
        "[sqlfluff:templater:jinja]\nloader_search_path = templates\n"
        "[sqlfluff:templater:jinja:context]\nvalue = 1\n")
    (project / "templates").mkdir()
    part = project / "templates/private_part.j2"
    part.write_text("SELECT ( FROM;\n")
    (project / "query.sql").write_text("{% include 'private_part.j2' %}\n")
    return part


def detail(result, reasons, changes):
    assert result.explanations == [{"filepath": "query.sql", "action": "linted",
                                   "reasons": reasons, "dependency_changes": changes}]


def test_include_edit_default_then_opt_in(included):
    cold = lint(["query.sql"])
    assert cold.exit_code == 1
    assert "dependency_changes" not in cold.explanations[0]
    included.write_text("SELECT 1;\n")
    changed = lint(["query.sql"], explain_dependencies=True)
    detail(changed, ["dependencies_changed"], ["template_files"])
    assert changed.stats.misses == 1 and changed.exit_code == 0
    assert stable(changed.records) == stable(direct(["query.sql"]))
    warm = lint(["query.sql"], explain_dependencies=True)
    assert warm.stats.hits == 1
    assert warm.explanations[0]["dependency_changes"] == []
    assert warm.records == changed.records


@pytest.mark.parametrize("component", ["python", "schema", "packages", "plugins"])
def test_runtime_and_plugin_subcategory(project, monkeypatch, component):
    lint(["clean.sql"])
    stamp = engine.engine_stamp()
    value = {"python": "different-runtime", "schema": 999,
             "packages": [*stamp["packages"], ["private_distribution", "private_version"]],
             "plugins": [*stamp["plugins"], ["private_plugin", "pkg:entry", "pkg", "private_version"]]}[component]
    monkeypatch.setattr(engine, "engine_stamp", lambda: {**stamp, component: value})
    result = lint(["clean.sql"], explain_dependencies=True)
    assert result.explanations[0]["reasons"] == ["dependencies_changed"]
    assert result.explanations[0]["dependency_changes"] == ["python_plugins"]
    assert "private_" not in json.dumps(result.explanations)


def test_trusted_environment_and_combined_changes(included, project, monkeypatch, capsys):
    library = project / "private_library"
    library.mkdir()
    (library / "__init__.py").write_text(
        "import os\ndef answer(): return os.environ.get('PRIVATE_INPUT_043', '1')\n")
    config = project / ".sqlfluff"
    config.write_text(config.read_text().replace("loader_search_path = templates",
                                               "loader_search_path = templates\nlibrary_path = private_library"))
    included.write_text("SELECT {{ answer() }};\n")
    monkeypatch.setenv("PRIVATE_INPUT_043", "1")
    # Isolate this test from Python's incidental bytecode creation.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    lint(["query.sql"], trust_template_code=True)
    assert lint(["query.sql"], trust_template_code=True).stats.hits == 1
    monkeypatch.setenv("PRIVATE_INPUT_043", "( FROM /*PRIVATE_VALUE_043*/")
    result = lint(["query.sql"], trust_template_code=True, explain_dependencies=True)
    detail(result, ["dependencies_changed"], ["environment"])
    assert result.exit_code == 1
    assert stable(result.records) == stable(lint(["query.sql"], trust_template_code=True, no_cache=True).records)
    included.write_text("SELECT {{ answer() }} AS changed;\n")
    monkeypatch.setenv("PRIVATE_INPUT_043", "2")
    stamp = engine.engine_stamp()
    monkeypatch.setattr(engine, "engine_stamp", lambda: {**stamp, "python": "private-runtime"})
    assert main(["lint", "query.sql", "--trust-template-code", "--explain-dependencies",
                 "--format", "cache-json"]) == 0
    output = capsys.readouterr()
    item = json.loads(output.out)["files"][0]
    assert item["reasons"] == ["dependencies_changed"]
    assert item["dependency_changes"] == ["template_files", "python_plugins", "environment"]
    assert "dependencies_changed [template_files, python_plugins, environment]" in output.err
    safe = output.out + output.err + ''.join(p.read_text() for p in
                                             (project / '.sqlfluff_cache').glob('.inputs-*'))
    for secret in ("PRIVATE_INPUT_043", "PRIVATE_VALUE_043", "private_library", "private_part.j2", "private-runtime"):
        assert secret not in safe


def test_unrelated_changes_have_no_subcategories(included, project, monkeypatch):
    lint(["query.sql"])
    monkeypatch.setenv("UNUSED_INPUT_043", "anything")  # ordinary Jinja doesn't track env
    warm = lint(["query.sql"], explain_dependencies=True)
    assert warm.stats.hits == 1 and warm.explanations[0]["dependency_changes"] == []
    config = project / ".sqlfluff"
    config.write_text(config.read_text().replace("value = 1", "value = 2"))
    detail(lint(["query.sql"], explain_dependencies=True), ["settings_changed"], [])
    stamp = engine.engine_stamp()
    monkeypatch.setattr(engine, "engine_stamp", lambda: {
        **stamp, "sqlfluff": "other", "packages": [(n, "other" if n.lower() == "sqlfluff" else v)
                                                   for n, v in stamp["packages"]],
        "plugins": [(*p[:-1], "other") if p[2].lower() == "sqlfluff" else p for p in stamp["plugins"]]})
    detail(lint(["query.sql"], explain_dependencies=True), ["sqlfluff_version_changed"], [])
    (project / "query.sql").write_text("SELECT 3;\n")
    detail(lint(["query.sql"], explain_dependencies=True), ["content_changed"], [])


def rewrite_history(project, edit):
    path = next((project / '.sqlfluff_cache').glob('.inputs-*'))
    history = json.loads(path.read_text())
    history.pop('checksum')
    edit(history)
    path.write_text(json.dumps({**history, 'checksum': digest(history)}))
    return path, history


def test_legacy_history_unknown_then_exact_hit_upgrade(included, project, monkeypatch):
    lint(["query.sql"])
    rewrite_history(project, lambda h: h.pop('dependency_components'))
    included.write_text("SELECT 2;\n")
    detail(lint(["query.sql"], explain_dependencies=True), ["dependencies_changed"], ["unavailable"])
    path, history = rewrite_history(project, lambda h: h.pop('dependency_components'))
    def forbidden(*args, **kwargs):
        pytest.fail('new explanation option changed the key or invoked rendering')
    monkeypatch.setattr(Linter, 'render_string', forbidden)
    warm = lint(["query.sql"], explain_dependencies=True)
    assert warm.stats.hits == 1 and warm.explanations[0]['dependency_changes'] == []
    assert json.loads(path.read_text())['key'] == history['key']
    assert set(json.loads(path.read_text())['dependency_components']) == {
        'template_files', 'python_plugins', 'environment'}
    assert lint(["query.sql"]).explanations[0] == {
        'filepath': 'query.sql', 'action': 'replayed', 'reasons': ['cache_hit']}


@pytest.mark.parametrize('damage', ['unknown_category', 'bad_hash'])
def test_invalid_details_recover_without_guessing(included, project, damage):
    lint(["query.sql"])
    def edit(h):
        if damage == 'unknown_category':
            h['dependency_components']['private_extra'] = '0' * 64
        else:
            h['dependency_components']['environment'] = 'not-a-hash'
    rewrite_history(project, edit)
    included.write_text("SELECT 4;\n")
    detail(lint(["query.sql"], explain_dependencies=True), ['history_unavailable'], [])
    assert lint(["query.sql"], explain_dependencies=True).stats.hits == 1


def test_noncomparison_events_and_formatter_boundary(project, capsys):
    result = lint(["clean.sql"], explain_dependencies=True)
    assert result.explanations[0]['dependency_changes'] == []
    history = next((project / '.sqlfluff_cache').glob('.inputs-*'))
    key = json.loads(history.read_text())['key']
    (history.parent / (key + '.json')).write_text('{')
    result = lint(["clean.sql"], explain_dependencies=True)
    assert result.explanations[0]['reasons'] == ['corrupt_entry']
    assert result.explanations[0]['dependency_changes'] == []
    assert main(['lint', 'clean.sql', '--format', 'json', '--explain-dependencies']) == 0
    output = capsys.readouterr()
    assert isinstance(json.loads(output.out), list)
    assert 'replayed "clean.sql": cache_hit' in output.err
    before = history.read_bytes()
    assert lint(['clean.sql'], no_cache=True, explain_dependencies=True).explanations[0]['dependency_changes'] == []
    assert history.read_bytes() == before
    (project / 'large.sql').write_text('SELECT 1;\n' * 3000)
    skipped = lint(['large.sql'], explain_dependencies=True).explanations[0]
    assert skipped['action'] == 'skipped' and skipped['dependency_changes'] == []
    (project / '.sqlfluff').write_text('[sqlfluff]\ndialect = ansi\ntemplater = jinja\n')
    (project / 'query.sql').write_text('SELECT {{ [1] | random }};\n')
    detail(lint(['query.sql'], explain_dependencies=True), ['template_cache_bypassed'], [])


def test_dbt_dependency_split_offline(project, monkeypatch):
    # Policy/fingerprints only: no adapter, credentials, code execution or DB.
    (project / 'dbt_project.yml').write_text('name: offline\n')
    config = FluffConfig(overrides={'dialect': 'ansi'})
    config.set_value(['templater', 'dbt', 'project_dir'], str(project))
    config.set_value(['templater', 'dbt', 'profiles_dir'], str(project / 'profiles'))

    def inputs():
        return Fingerprinter(project / '.sqlfluff_cache').template_inputs(
            config, 'dbt', trust_code=True, dbt_project_hash=True)

    before = inputs()
    monkeypatch.setenv('PRIVATE_DBT_INPUT_043', 'PRIVATE_VALUE_043')
    environment = inputs()
    assert environment.dependencies != before.dependencies
    assert environment.environment != before.environment
    assert environment.template_files == before.template_files
    (project / 'seed.csv').write_text('id\n1\n')
    files = inputs()
    assert files.dependencies != environment.dependencies
    assert files.environment == environment.environment
    assert files.template_files != environment.template_files
