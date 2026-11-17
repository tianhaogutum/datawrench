"""Top-level `datawrench` dispatcher: overview, version, routing and error paths."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

import datawrench
from datawrench import cli

ROOT = Path(__file__).resolve().parents[1]

# Commands whose Python subpackage ships in this repository (`check` is the hero command).
TOOLS = ["check", "contract-diff", "sqlmesh-manifest", "lint-cache", "dialect-audit", "rebuild-check",
         "join-witness", "grain-contract", "key-audit", "polars-vet", "parquet-unify", "seedtypes",
         "duckmate", "csv-rewind", "failurepack", "tapcheck", "singer-replay"]
# Every merged tool is built on argparse: `--help` prints a "usage: datawrench <tool>" line.
ARGPARSE_TOOLS = [tool for tool in TOOLS if tool != "check"]
# `check` keeps its own flags; every merged tool prints the bare package version.
VERSIONED = ARGPARSE_TOOLS
SUBCOMMANDS = {
    "sqlmesh-manifest": ["export", "validate", "summary", "validate-report", "validate-diff", "diff"],
    "lint-cache": ["lint", "status", "clear", "gc"],
    "dialect-audit": ["run", "baseline", "diff"],
    "polars-vet": ["check"],
    "parquet-unify": ["scan", "plan", "recipe", "verify", "rewrite", "validate"],
    "seedtypes": ["infer", "compare", "emit", "validate-types", "check"],
    "duckmate": ["new", "up", "migrate", "rollback", "status", "dump", "load", "verify", "plan"],
    "tapcheck": ["validate", "quality", "convert"],
    # `tap` is hand-routed (formerly the separate `tap-replay` console script).
    "singer-replay": ["record", "trim", "verify", "scrub", "tap"],
    "grain-contract": ["compile", "check", "from-dbt"],
    "key-audit": ["rules", "audit", "clean", "show", "export-map"],
    "failurepack": ["list", "reduce", "replay", "resume"],
}
PACKAGES = {
    "contract-diff": "datawrench.contract_diff", "sqlmesh-manifest": "datawrench.sqlmesh_manifest",
    "lint-cache": "datawrench.lint_cache", "dialect-audit": "datawrench.dialect_audit",
    "polars-vet": "datawrench.polars_vet", "parquet-unify": "datawrench.parquet_unify",
    "seedtypes": "datawrench.seedtypes", "duckmate": "datawrench.duckmate",
    "tapcheck": "datawrench.tapcheck", "singer-replay": "datawrench.singer_replay",
    "rebuild-check": "datawrench.rebuild_check", "join-witness": "datawrench.join_witness",
    "grain-contract": "datawrench.grain_contract", "key-audit": "datawrench.key_audit",
    "csv-rewind": "datawrench.csv_rewind", "failurepack": "datawrench.failurepack",
}
EXTRAS = {
    "contract-diff": "sqlglot", "sqlmesh-manifest": "sqlmesh", "lint-cache": "sqlfluff",
    "dialect-audit": "sqlglot,duckdb", "parquet-unify": "parquet,duckdb,polars",
    "seedtypes": "duckdb,polars,sqlglot", "duckmate": "duckdb", "tapcheck": "singer",
    "rebuild-check": "sqlite", "join-witness": "tableproof", "grain-contract": "sqlglot,ijson",
    "key-audit": "ftfy", "csv-rewind": "duckdb", "failurepack": "pandera",
}
LEGACY_NAMES = [
    ("dbt-contract-diff", "contract-diff"), ("sqlmesh-dbt-manifest", "sqlmesh-manifest"),
    ("sqlfluff-cache", "lint-cache"), ("tap-replay", "singer-replay tap"),
    ("sqlite-rebuild-check", "rebuild-check"), ("sqlite-utils-rebuild-check", "rebuild-check"),
    ("keyaudit", "key-audit"), ("unicode-key-audit", "key-audit"),
    ("sql-grain-contract", "grain-contract"), ("tableproof-join-witness", "join-witness"),
]
# sqlmesh-manifest's extra is not part of `test` (CI runs it in a separate job) and failurepack's
# `pandera` extra installs only on Python 3.12+; skip the commands whose optional dependencies are
# absent instead of failing on the install hint.
OPTIONAL = {"sqlmesh-manifest": ("sqlmesh", "dbt_artifacts_parser"), "failurepack": ("dfshrink", "pandera")}


def _skip_without_extra(tool):
    for module in OPTIONAL.get(tool, ()):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"{tool} needs {module} (install the '{EXTRAS[tool]}' extra)")


def run(*args, module=("-m", "datawrench")):
    return subprocess.run([sys.executable, *module, *args], capture_output=True, text=True, timeout=60)


def _main(argv, capsys):
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["help"]])
def test_overview_lists_every_group_and_command(args, capsys):
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"datawrench {datawrench.__version__} ")
    for group in cli.GROUPS:
        assert f"\n{group}:\n" in out
    for name in TOOLS:
        assert f"\n  {name} " in out


def test_command_table_shape():
    groups = {group: [c.name for c in cli.COMMANDS if c.group == group] for group in cli.GROUPS}
    assert groups == {
        "Check": ["check"],
        "dbt & SQLMesh": ["contract-diff", "sqlmesh-manifest"],
        "SQL quality": ["lint-cache", "dialect-audit", "rebuild-check"],
        "Keys & joins": ["join-witness", "grain-contract", "key-audit"],
        "DataFrames & files": ["polars-vet", "parquet-unify", "seedtypes", "duckmate", "csv-rewind",
                               "failurepack"],
        "Singer pipelines": ["tapcheck", "singer-replay"],
    }
    assert {c.name: c.module for c in cli.COMMANDS} == {
        "check": "datawrench.check.cli",
        **{name: f"{package}.cli" for name, package in PACKAGES.items()},
    }
    assert all(c.group in cli.GROUPS and c.summary for c in cli.COMMANDS)
    assert set(cli.BY_NAME) == {c.name for c in cli.COMMANDS}
    assert {c.name: c.extra for c in cli.COMMANDS if c.extra} == EXTRAS
    assert all(bool(c.requires) == bool(c.extra) for c in cli.COMMANDS)


def test_extras_are_declared_in_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = {part for c in cli.COMMANDS if c.extra for part in c.extra.split(",")}
    for extra in declared | {"all", "test", "flake8"}:
        assert f"\n{extra} = [" in text, extra


def test_sqlite_utils_plugin_entry_point_targets_shipped_module():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'rebuild_check = "datawrench.rebuild_check.plugin"' in text
    assert importlib.util.find_spec("datawrench.rebuild_check.plugin") is not None


def test_flake8_plugin_entry_point_targets_shipped_module():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'PLV = "datawrench.polars_vet.plugin:PolarsVetChecker"' in text
    assert importlib.util.find_spec("datawrench.polars_vet.plugin") is not None


def test_shipped_tools_are_installed():
    for name in ARGPARSE_TOOLS:
        assert cli._installed(cli.BY_NAME[name]), name


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out == f"datawrench {datawrench.__version__}\n"


def _assert_tool_help(tool, out):
    assert f"datawrench {tool}" in out
    if tool in ARGPARSE_TOOLS:
        assert f"usage: datawrench {tool}" in out


@pytest.mark.parametrize("tool", ARGPARSE_TOOLS)
def test_tool_help_uses_new_program_name(tool):
    _skip_without_extra(tool)
    result = run(tool, "--help")
    assert result.returncode == 0, result.stderr
    _assert_tool_help(tool, result.stdout)


@pytest.mark.parametrize("tool", TOOLS)
def test_help_subcommand_routes_to_tool_help(tool, capsys):
    _skip_without_extra(tool)
    code, out, _ = _main(["help", tool], capsys)
    assert code == 0
    _assert_tool_help(tool, out)


@pytest.mark.parametrize("tool", VERSIONED)
def test_tool_version_is_package_version(tool, capsys):
    _skip_without_extra(tool)
    code, out, _ = _main([tool, "--version"], capsys)
    assert code == 0
    assert out == f"{datawrench.__version__}\n"


@pytest.mark.parametrize("command,sub", [(c, s) for c, subs in SUBCOMMANDS.items() for s in subs])
def test_subcommand_help_uses_new_program_name(command, sub, capsys):
    _skip_without_extra(command)
    code, out, err = _main([command, sub, "--help"], capsys)
    assert code == 0, err
    assert f"usage: datawrench {command} {sub}" in out


@pytest.mark.parametrize("tool,package", sorted(PACKAGES.items()))
def test_subpackage_main_routes_through_dispatcher(tool, package):
    _skip_without_extra(tool)
    result = run("--help", module=("-m", package))
    assert result.returncode == 0, result.stderr
    assert f"usage: datawrench {tool}" in result.stdout


def test_singer_replay_help_mentions_tap(capsys):
    code, out, _ = _main(["singer-replay", "--help"], capsys)
    assert code == 0 and "datawrench singer-replay tap" in out


def test_singer_replay_tap_version_is_package_version(capsys):
    code, out, _ = _main(["singer-replay", "tap", "--version"], capsys)
    assert code == 0 and out == f"{datawrench.__version__}\n"


@pytest.mark.parametrize("tool", ["polars-vet", "singer-replay", "duckmate", "tapcheck", "seedtypes",
                                  "join-witness", "grain-contract", "key-audit"])
def test_tool_exit_codes_pass_through(tool):
    bad = run(tool, "no-such-subcommand")
    assert bad.returncode == 2 and f"usage: datawrench {tool}" in bad.stderr
    assert "Traceback" not in bad.stderr


def test_unknown_command_suggests_close_match(capsys):
    assert cli.main(["tapchek"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'tapchek'" in err and "did you mean 'tapcheck'?" in err


def test_unknown_legacy_typo_suggests_new_command(capsys):
    assert cli.main(["sqlfluff-cach"]) == 2
    assert "did you mean 'lint-cache'?" in capsys.readouterr().err


@pytest.mark.parametrize("legacy,new", LEGACY_NAMES)
def test_unknown_legacy_name_points_to_new_command(legacy, new, capsys):
    assert cli.main([legacy]) == 2
    assert f"{legacy} is now 'datawrench {new}'" in capsys.readouterr().err


def test_unknown_command_without_match(capsys):
    assert cli.main(["zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'zzzzzz'" in err and "did you mean" not in err


def test_unknown_option(capsys):
    assert cli.main(["--frobnicate"]) == 2
    assert "unknown option '--frobnicate'" in capsys.readouterr().err


def test_missing_module_reports_not_available(monkeypatch, capsys):
    ghost = cli.Command("ghost", "datawrench.ghost_tool.cli", "not shipped", "Check")
    monkeypatch.setitem(cli.BY_NAME, "ghost", ghost)
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, ghost))
    overview = cli.overview()
    assert "ghost" in overview and "(coming soon)" in overview
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("datawrench: command 'ghost' is not available") and "Traceback" not in err


def test_missing_optional_dependency_prints_install_hint(monkeypatch, capsys):
    command = cli.Command("ghost", "datawrench.singer_replay.cli", "needs a dependency", "Check",
                          requires=("ghostdep",), extra="ghostextra,other")
    monkeypatch.setitem(cli.BY_NAME, "ghost", command)

    def missing(name):
        raise ModuleNotFoundError("No module named 'ghostdep.sub'", name="ghostdep.sub")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err == ("datawrench ghost needs ghostdep: pipx install 'datawrench[ghostextra,other]' "
                   "(or pip install 'datawrench[ghostextra,other]')\n")


def test_unrelated_module_error_is_not_masked(monkeypatch):
    command = cli.Command("ghost", "datawrench.singer_replay.cli", "needs a dependency", "Check",
                          requires=("ghostdep",), extra="ghostextra")
    monkeypatch.setitem(cli.BY_NAME, "ghost", command)

    def missing(name):
        raise ModuleNotFoundError("No module named 'other'", name="other")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    with pytest.raises(ModuleNotFoundError):
        cli.main(["ghost"])


# Simulate an install without the extra: a None entry in sys.modules makes the import fail.
_WITHOUT = ("import sys\nfor name in {blocked!r}: sys.modules[name] = None\n"
            "from datawrench.cli import main\nraise SystemExit(main({argv!r}))\n")


@pytest.mark.parametrize("tool,blocked", [
    ("contract-diff", ["sqlglot"]),
    ("sqlmesh-manifest", ["sqlmesh", "dbt_artifacts_parser", "ruamel", "pydantic"]),
    ("lint-cache", ["sqlfluff"]),
    ("dialect-audit", ["sqlglot", "duckdb"]),
    ("parquet-unify", ["pyarrow"]),
    ("seedtypes", ["duckdb"]),
    ("duckmate", ["duckdb"]),
    ("tapcheck", ["jsonschema"]),
    ("rebuild-check", ["sqlite_utils", "sqlparse"]),
    ("join-witness", ["tableproof"]),
    ("grain-contract", ["sqlglot"]),
    ("key-audit", ["ftfy"]),
    ("csv-rewind", ["duckdb"]),
    ("failurepack", ["pandera"]),
])
def test_missing_extra_exits_2_with_one_line_hint(tool, blocked, tmp_path):
    result = subprocess.run([sys.executable, "-c", _WITHOUT.format(blocked=blocked, argv=[tool, "--help"])],
                            cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2, result.stderr
    assert result.stderr.startswith(f"datawrench {tool} needs ")
    assert f": pipx install 'datawrench[{EXTRAS[tool]}]'" in result.stderr
    assert result.stderr.count("\n") == 1
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("tool", ["polars-vet", "singer-replay"])
def test_standard_library_tools_need_no_extra(tool, tmp_path):
    blocked = ["sqlglot", "duckdb", "polars", "pyarrow", "sqlfluff", "jinja2", "jsonschema",
               "sqlmesh", "flake8", "sqlite_utils", "sqlparse", "tableproof", "ijson", "ftfy", "pandera",
               "pandas", "dfshrink"]
    result = subprocess.run([sys.executable, "-c", _WITHOUT.format(blocked=blocked, argv=[tool, "--help"])],
                            cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert f"usage: datawrench {tool}" in result.stdout


@pytest.mark.parametrize("name", TOOLS)
def test_shipped_command_is_listed_without_coming_soon(name, capsys):
    if not cli._installed(cli.BY_NAME[name]):
        pytest.skip(f"{name} has not landed yet")
    assert cli.main([]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith(f"  {name} "))
    assert "(coming soon)" not in line
