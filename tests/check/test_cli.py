"""``datawrench check`` command line: formats, exit codes, selection, the README demo."""
import json
import re

import pytest

from datawrench import __version__
from datawrench.check import checks as checks_module
from datawrench.check.cli import parse_dialect
from datawrench.check.registry import AREAS, CHECKS

from .conftest import DEMO_EXTRAS, findings, needs, rule_ids, snapshot, write

SUMMARY = re.compile(r"^\d+ inputs? · \d+ errors? · \d+ warnings? · \d+ info$", re.MULTILINE)
BASE = ("--base-manifest", "prod/manifest.json")


def test_help_uses_program_name(check):
    code, out, _ = check("--help")
    assert code == 0
    assert "usage: datawrench check" in out
    assert "--base-manifest" in out and "--dialect" in out
    assert "GITHUB_STEP_SUMMARY" in out


def test_list_checks_names_every_check_by_area(check):
    code, out, _ = check("--list-checks")
    assert code == 0
    for area in AREAS:
        assert f"\n{area}:\n" in out
    for item in CHECKS:
        assert re.search(rf"^  {re.escape(item.name)}\s", out, re.MULTILINE), item.name
    assert "needs datawrench[parquet]" in out
    assert "needs --dialect FROM:TO" in out
    assert "compares only with --base-manifest PATH" in out


def test_readme_demo_output(demo, check):
    """The lines shown (illustratively) in assets/check-demo.svg and the README."""
    needs(*DEMO_EXTRAS)
    code, out, err = check(*BASE, "--color", "never")
    assert code == 1 and err == ""
    assert out.startswith("datawrench check  .  (8 inputs: 1 dbt project, 2 dbt manifests, 2 SQL files, "
                          "1 Polars script, 1 migrations folder, 1 Singer/Airbyte message file; "
                          "11 files inspected)\n")
    for area in ("dbt & SQLMesh", "SQL quality", "DataFrames & files", "Singer pipelines"):
        assert f"\n{area}\n" in out
    expected = [
        r"error  contract-diff/breaking +model\.shop\.orders › coupon_code: column removed$",
        r"error  contract-diff/breaking +model\.shop\.orders › customer_id: bigint -> integer \(narrowing\)$",
        r"info   contract-diff/non-breaking +model\.shop\.orders › ordered_at: column added$",
        r"info   contract-diff/summary +compared with prod/manifest\.json \(duckdb types\): 1 model with "
        r"enforced contracts, 3 changes, 2 breaking$",
        r"info   dialect-audit/needs-dialect +2 SQL files not audited for a dialect migration: no dialect pair "
        r"given; the dbt manifest says the source dialect is duckdb$",
        r"info   lint-cache/needs-config +2 SQL files not linted: no SQLFluff configuration",
        r"warn   polars-vet/PLV001 +Eager file read immediately converted to lazy; use a scan reader  line 6$",
        r"warn   polars-vet/PLV004 +with_columns in a loop; batch independent expressions outside the loop  line 9$",
        r"warn   polars-vet/PLV009 +Full sort followed by head; consider top_k/bottom_k, then sort the result; "
        r"check null ordering and ties; no automatic rewrite  line 10$",
        r"warn   seedtypes/unpinned-lossy +zip: default Polars CSV read alters values; the seed has no "
        r"column_types pin for it  line 2$",
        r"error  duckmate/unsupported-statement +migrate:up: statement 2: secrets and temporary objects are "
        r"unsupported$",
        r"info   duckmate/empty-down +the migrate:down section is empty: a rollback will not undo this migration$",
        r"error  tapcheck/TC006 +stream orders: record violates JSON Schema \(type\) at amount  line 4$",
        r"error  tapcheck/TC010 +stream orders: key property missing or null at id  line 3$",
        r"warn   tapcheck/TC008 +stream orders: missing final STATE covering records for stream  line 4$",
    ]
    for pattern in expected:
        assert re.search(r"^    " + pattern, out, re.MULTILINE), pattern
    assert "\n8 inputs · 5 errors · 5 warnings · 8 info\n" in out
    assert "\x1b[" not in out


def test_demo_without_base_manifest_asks_for_one(demo, check_json):
    needs(*DEMO_EXTRAS)
    _, document = check_json()
    asked = findings(document, "contract-diff/needs-base")
    assert sorted(f["file"] for f in asked) == ["prod/manifest.json", "target/manifest.json"]
    assert "1 model with an enforced contract" in asked[0]["message"]
    assert not findings(document, "contract-diff/breaking")


def test_demo_json_document(demo, check_json):
    needs(*DEMO_EXTRAS)
    code, document = check_json(*BASE)
    assert code == 1
    assert document["format"] == "datawrench.check/v1"
    assert document["tool"] == {"name": "datawrench", "version": __version__}
    assert document["summary"] == {"inputs": 8, "inspected": 11, "error": 5, "warning": 5, "info": 8}
    assert document["failOn"] == "error" and document["failed"] is True
    areas = {f["ruleId"]: f["area"] for f in document["findings"]}
    assert areas["contract-diff/breaking"] == "dbt & SQLMesh"
    assert areas["polars-vet/PLV004"] == "DataFrames & files"
    assert areas["tapcheck/TC010"] == "Singer pipelines"
    kinds = {item["path"]: item["kind"] for item in document["inputs"]}
    assert kinds["."] == "dbt-project" and kinds["db/migrations"] == "duckmate-migrations"
    assert "seeds/zip_codes.csv" not in kinds  # a seed belongs to its dbt project
    seed = findings(document, "seedtypes/unpinned-lossy")[0]
    assert seed["file"] == "seeds/zip_codes.csv" and seed["subject"] == "zip" and seed["line"] == 2


def test_check_writes_nothing(demo, check):
    needs(*DEMO_EXTRAS)
    before = snapshot(demo)
    check(*BASE, "--dialect", ":snowflake")
    assert snapshot(demo) == before
    assert not (demo / ".sqlfluff_cache").exists()


def test_markdown_for_step_summary(demo, check):
    needs(*DEMO_EXTRAS)
    code, out, err = check(*BASE, "--format", "markdown")
    assert code == 1 and err == ""
    assert out.startswith("## datawrench check\n")
    assert "**Failed** (fail on: error) · 5 errors · 5 warnings · 8 info · 8 inputs checked" in out
    assert "### dbt & SQLMesh" in out and "### Singer pipelines" in out
    assert "| **error** | `contract-diff/breaking` | `target/manifest.json` |" in out
    assert "| warning | `polars-vet/PLV004` | `pipelines/daily_revenue.py:9` |" in out
    assert "<details><summary>" in out
    assert "\x1b[" not in out


def test_sarif_document(demo, check):
    needs(*DEMO_EXTRAS)
    code, out, _ = check(*BASE, "--format", "sarif")
    document = json.loads(out)
    assert code == 1
    assert document["version"] == "2.1.0"
    run = document["runs"][0]
    assert run["tool"]["driver"]["name"] == "datawrench"
    results = {r["ruleId"]: r for r in run["results"]}
    located = results["polars-vet/PLV004"]["locations"][0]["physicalLocation"]
    assert located["artifactLocation"] == {"uri": "pipelines/daily_revenue.py", "uriBaseId": "%SRCROOT%"}
    assert located["region"] == {"startLine": 9}
    assert results["contract-diff/breaking"]["level"] == "error"
    assert results["tapcheck/TC008"]["level"] == "warning"
    # Findings about folders (the dbt project, the migrations folder) and the whole run are notifications.
    notes = [n["message"]["text"] for n in run["invocations"][0]["toolExecutionNotifications"]]
    assert any(text.startswith("seedtypes/summary (.)") for text in notes)
    assert any(text.startswith("lint-cache/needs-config:") for text in notes)
    ids = [rule["id"] for rule in run["tool"]["driver"]["rules"]]
    assert len(ids) == len(set(ids))
    assert run["invocations"][0]["executionSuccessful"] is True


@pytest.mark.parametrize("fail_on, expected", [("error", 1), ("warning", 1), ("never", 0)])
def test_fail_on(demo, check, fail_on, expected):
    needs(*DEMO_EXTRAS)
    assert check(*BASE, "--fail-on", fail_on, "--color", "never")[0] == expected


def test_warnings_only_pass_by_default(demo, check):
    code, _, _ = check("--only", "polars-vet")
    assert code == 0
    assert check("--only", "polars-vet", "--fail-on", "warning")[0] == 1


def test_only_and_skip(demo, check_json):
    _, document = check_json("--only", "polars-vet")
    assert {f["check"] for f in document["findings"]} == {"polars-vet"}
    assert document["checks"] == ["polars-vet"]
    _, document = check_json("--skip", "tapcheck", "--skip", "duckmate,seedtypes,contract-diff")
    assert not {"tapcheck", "duckmate", "seedtypes", "contract-diff"} & {f["check"] for f in document["findings"]}


@pytest.mark.parametrize("argv, message", [
    (("--only", "nope"), "unknown check(s): nope"),
    (("--only", "polars-vet", "--skip", "polars-vet"), "leave no check to run"),
    (("missing-folder",), "no such file or directory: missing-folder"),
    (("--sql-schema", "dbt_project.yml"), "--sql-schema needs --dialect"),
])
def test_usage_errors_exit_2(demo, check, argv, message):
    code, out, err = check(*argv)
    assert code == 2 and out == ""
    assert message in err


@pytest.mark.parametrize("value", ["snowflake", "snowflake:", "a b:duckdb", ":"])
def test_bad_dialect_pair_is_a_usage_error(check, value):
    code, _, err = check("--dialect", value)
    assert code == 2 and "FROM:TO" in err


def test_parse_dialect():
    assert parse_dialect("Snowflake:DuckDB") == ("snowflake", "duckdb")
    assert parse_dialect(":duckdb") == (None, "duckdb")


def test_empty_folder(workdir, check):
    code, out, _ = check("--color", "never")
    assert code == 0
    assert "Nothing to check here" in out
    assert SUMMARY.search(out)


def test_crashing_check_becomes_internal_finding(demo, check_json, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret content must not be echoed")

    monkeypatch.setattr("datawrench.polars_vet.analysis.analyze", boom)
    code, document = check_json("--only", "polars-vet,tapcheck")
    internal = findings(document, "polars-vet/internal")
    assert len(internal) == 1 and internal[0]["severity"] == "error"
    assert "RuntimeError" in internal[0]["message"] and "secret" not in internal[0]["message"]
    assert code == 1


def test_missing_extra_becomes_one_info_with_install_hint(demo, check_json, monkeypatch):
    real = checks_module.available
    monkeypatch.setattr(checks_module, "available", lambda module: module != "jsonschema" and real(module))
    _, document = check_json("--only", "tapcheck")
    missing = findings(document, "tapcheck/missing-dependency")
    assert len(missing) == 1 and missing[0]["severity"] == "info"
    assert "jsonschema is not installed" in missing[0]["message"]
    assert missing[0]["hint"].startswith("pipx install 'datawrench[singer]'")


def test_untrusted_text_is_escaped(workdir, check):
    write(workdir / "bad\x1b[31m.py", "import polars as pl\nq = pl.read_csv('x.csv').lazy()\n")
    _, out, _ = check("--color", "never")
    assert "\x1b[31m" not in out and "\\x1b[31m" in out
