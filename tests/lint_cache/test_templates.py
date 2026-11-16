import shutil
from pathlib import Path

import pytest
from sqlfluff.core import FluffConfig, Linter

from datawrench.lint_cache.engine import lint
from datawrench.lint_cache.fingerprint import Fingerprinter, InputError
from .conftest import FIXTURES
from .test_engine import direct, stable


@pytest.fixture
def jinja(tmp_path, monkeypatch):
    root = tmp_path / "jinja"
    shutil.copytree(FIXTURES / "jinja", root)
    monkeypatch.chdir(root)
    return root


def test_jinja_macro_context_invalidation(jinja):
    expected = direct(["query.sql"])
    cold = lint(["query.sql"])
    assert stable(cold.records) == stable(expected)
    assert lint(["query.sql"]).stats.hits == 1
    macro = jinja / "macros/value.sql"
    macro.write_text("{% macro value() %}( FROM{% endmacro %}\n")
    changed = lint(["query.sql"])
    assert changed.stats.misses == 1
    assert stable(changed.records) == stable(direct(["query.sql"]))
    assert changed.records[0]["violations"]
    config = jinja / ".sqlfluff"
    config.write_text(config.read_text().replace("label = value", "label = another"))
    assert lint(["query.sql"]).stats.misses == 1


def test_jinja_loader_search_path(jinja):
    (jinja / "templates").mkdir()
    (jinja / "templates/select.sql").write_text("SELECT 1 AS value;\n")
    config = jinja / ".sqlfluff"
    config.write_text(config.read_text().replace("load_macros_from_path = macros", "load_macros_from_path = macros\nloader_search_path = templates"))
    (jinja / "query.sql").write_text("{% include 'select.sql' %}")
    assert lint(["query.sql"]).stats.misses == 1
    assert lint(["query.sql"]).stats.hits == 1
    (jinja / "templates/select.sql").write_text("SELECT  2 AS value;\n")
    assert lint(["query.sql"]).stats.misses == 1


@pytest.mark.parametrize("templater,sql,section", [
    ("python", "SELECT {value} AS item;\n", "[sqlfluff:templater:python:context]\nvalue = 1\n"),
    ("placeholder", "SELECT :value AS item;\n", "[sqlfluff:templater:placeholder]\nparam_style = colon\nvalue = 1\n"),
])
def test_other_builtin_templaters(tmp_path, monkeypatch, templater, sql, section):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".sqlfluff").write_text(f"[sqlfluff]\ndialect = ansi\ntemplater = {templater}\n" + section)
    (tmp_path / "query.sql").write_text(sql)
    result = lint(["query.sql"])
    assert stable(result.records) == stable(direct(["query.sql"]))
    assert lint(["query.sql"]).stats.hits == 1
    config = tmp_path / ".sqlfluff"
    config.write_text(config.read_text().replace("value = 1", "value = 2"))
    assert lint(["query.sql"]).stats.misses == 1


def test_library_requires_explicit_trust_and_hashes_code(jinja):
    lib = jinja / "library"
    lib.mkdir()
    (lib / "__init__.py").write_text("from pathlib import Path\nPath('executed').write_text('yes')\ndef answer(): return 1\n")
    cfg = jinja / ".sqlfluff"
    cfg.write_text(cfg.read_text().replace("load_macros_from_path = macros", "load_macros_from_path = macros\nlibrary_path = library"))
    (jinja / "query.sql").write_text("SELECT {{ answer() }} AS value;\n")
    with pytest.raises(InputError, match="executable code"):
        lint(["query.sql"])
    assert not (jinja / "executed").exists()
    assert lint(["query.sql"], trust_template_code=True).stats.misses == 1
    assert (jinja / "executed").exists()
    # Python generated __pycache__ on the first import. It is an input too,
    # so one additional cold run establishes a stable dependency tree.
    assert lint(["query.sql"], trust_template_code=True).stats.misses == 1
    assert lint(["query.sql"], trust_template_code=True).stats.hits == 1
    # Use a different length to avoid Python's timestamp/size pyc reuse.
    (lib / "__init__.py").write_text("def answer(): return 12345\n")
    assert lint(["query.sql"], trust_template_code=True).stats.misses == 1


def test_symlink_dependency_rejected(jinja, tmp_path):
    external = tmp_path / "external.sql"
    external.write_text("sensitive text")
    (jinja / "macros/link.sql").symlink_to(external)
    with pytest.raises(InputError, match="symlink"):
        lint(["query.sql"])


def test_dbt_fingerprint_bypass_and_whole_project_hash(jinja, monkeypatch):
    # Offline unit test of the policy; no dbt adapter, credentials or database.
    config = FluffConfig(overrides={"dialect": "ansi"})
    config.set_value(["templater", "dbt", "project_dir"], str(jinja))
    config.set_value(["templater", "dbt", "profiles_dir"], str(jinja / "profiles"))
    fp = lambda: Fingerprinter(jinja / ".sqlfluff_cache")
    with pytest.raises(InputError, match="cache bypassed"):
        fp().templater(config, "dbt", trust_code=False, dbt_project_hash=False)
    key, enabled, notice = fp().templater(config, "dbt", trust_code=True, dbt_project_hash=False)
    assert not enabled and "cache bypassed" in notice
    with pytest.raises(InputError, match="dbt_project.yml"):
        fp().templater(config, "dbt", trust_code=True, dbt_project_hash=True)
    (jinja / "dbt_project.yml").write_text("name: offline_project\n")
    def fingerprint():
        return fp().templater(config, "dbt", trust_code=True, dbt_project_hash=True)[0]
    first = fingerprint()
    (jinja / "seed.csv").write_text("id\n1\n")
    second = fingerprint()
    assert first != second
    (jinja / "profiles").mkdir()
    (jinja / "profiles/profiles.yml").write_text("offline: true\n")
    third = fingerprint()
    assert third != second
    monkeypatch.setenv("DBT_TEST_INPUT", "changed")
    assert fingerprint() != third
    # Writing the cache itself must not invalidate the dbt project hash.
    before = fingerprint()
    (jinja / ".sqlfluff_cache").mkdir()
    (jinja / ".sqlfluff_cache/entry").write_text("cached result")
    assert fingerprint() == before


def test_dbt_engine_bypass_notice_without_dbt_execution(project, monkeypatch):
    # Exercise orchestration with an offline stand-in, not claim dbt integration.
    original = Linter.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.templater.name = "dbt"
    monkeypatch.setattr(Linter, "__init__", init)
    notices = []
    for _ in range(2):
        result = lint(["clean.sql"], trust_template_code=True, warn=notices.append)
        assert result.stats.bypassed == result.stats.misses == 1
        assert not result.stats.hits
    assert any("cache bypassed" in message for message in notices)
    assert not (project / ".sqlfluff_cache").exists()


@pytest.mark.parametrize("expression", ["[1, 2] | random", "[[1, 2]] | map('random') | first"])
def test_nondeterministic_jinja_bypasses_cache(jinja, expression):
    (jinja / "query.sql").write_text("SELECT {{ " + expression + " }};\n")
    notices = []
    for _ in range(2):
        result = lint(["query.sql"], warn=notices.append)
        assert result.stats.bypassed == 1
        assert result.stats.hits == 0
    assert any("random input" in message for message in notices)
    assert not (jinja / ".sqlfluff_cache").exists()


def test_nondeterministic_macro_bypasses(jinja):
    (jinja / "macros/value.sql").write_text("{% macro value() %}{{ [1, 2] | random }}{% endmacro %}\n")
    assert lint(["query.sql"]).stats.bypassed == 1


def test_template_cannot_execute_shell(jinja):
    # SQLFluff's Jinja sandbox must continue to own rendering; no eval fallback.
    (jinja / "query.sql").write_text("SELECT {{ cycler.__init__.__globals__.os.system('touch escaped') }};\n")
    result = lint(["query.sql"])
    assert result.exit_code == 1
    assert any(v["code"] == "TMP" for r in result.records for v in r["violations"])
    assert not (jinja / "escaped").exists()


def test_template_cannot_include_parent_file(jinja):
    parent = jinja.parent / "outside.sql"
    parent.write_text("SELECT 123456789;\n")
    (jinja / "query.sql").write_text("{% include '../outside.sql' %}")
    result = lint(["query.sql"])
    assert result.exit_code == 1
    assert all("123456789" not in v["description"] for r in result.records for v in r["violations"])


def test_dependency_byte_budget_is_enforced(jinja, monkeypatch):
    from datawrench.lint_cache import fingerprint
    monkeypatch.setattr(fingerprint, "MAX_TREE_BYTES", 4)
    with pytest.raises(InputError, match="exceed"):
        lint(["query.sql"])
    assert not (jinja / ".sqlfluff_cache").exists()


def test_dependency_file_budget_is_enforced(jinja, monkeypatch):
    from datawrench.lint_cache import fingerprint
    monkeypatch.setattr(fingerprint, "MAX_TREE_FILES", 0)
    with pytest.raises(InputError, match="exceed"):
        lint(["query.sql"])


def test_inline_macro_definition_order_invalidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    header = "[sqlfluff]\ndialect = ansi\ntemplater = jinja\n[sqlfluff:templater:jinja:macros]\n"
    definitions = ["a = {% macro chosen() %}1{% endmacro %}\n",
                   "b = {% macro chosen() %}( FROM{% endmacro %}\n"]
    config = tmp_path / ".sqlfluff"
    config.write_text(header + "".join(definitions))
    (tmp_path / "query.sql").write_text("SELECT {{ chosen() }};\n")
    before = lint(["query.sql"])
    assert before.exit_code == 1
    config.write_text(header + "".join(reversed(definitions)))
    after = lint(["query.sql"])
    assert after.stats.misses == 1
    assert after.exit_code == 0
    assert stable(after.records) == stable(direct(["query.sql"]))


def test_dependency_symlink_parent_rejected(jinja):
    (jinja / "real").mkdir()
    (jinja / "real/macros").mkdir()
    (jinja / "link").symlink_to(jinja / "real", target_is_directory=True)
    config = jinja / ".sqlfluff"
    config.write_text(config.read_text().replace("load_macros_from_path = macros", "load_macros_from_path = link/macros"))
    with pytest.raises(InputError, match="symlink"):
        lint(["query.sql"])
