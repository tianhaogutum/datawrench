"""Each check on small synthetic inputs, through the tools' own Python APIs."""
import io
import json

import pytest

from .conftest import (SINGER_STATE, findings, jsonl, manifest, needs, rule_ids, singer_record, singer_schema,
                       snapshot, write, write_json)


# --------------------------------------------------------------------------- polars-vet

def test_polars_vet_reports_rules_with_lines(workdir, check_json):
    write(workdir / "job.py", "import polars as pl\n\nframe = pl.read_csv('x.csv')\n"
                              "rows = [r for r in frame.iter_rows()]\n"
                              "top = frame.sort('a').head(3)\n")
    _, document = check_json("--only", "polars-vet")
    assert [(f["rule"], f["line"]) for f in document["findings"]] == [("PLV003", 4), ("PLV009", 5)]
    assert all(f["severity"] == "warning" for f in document["findings"])


def test_polars_vet_syntax_error_is_not_a_crash(workdir, check_json):
    write(workdir / "broken.py", "import polars as pl\ndef (:\n")
    _, document = check_json("--only", "polars-vet")
    (finding,) = document["findings"]
    assert finding["ruleId"] == "polars-vet/PLV000" and finding["message"].startswith("not fully analysed")


# --------------------------------------------------------------------------- contract-diff

def test_contract_diff_needs_a_base(workdir, check_json):
    write_json(workdir / "target/manifest.json", manifest({"id": "integer"}))
    write_json(workdir / "unenforced/manifest.json", manifest({"id": "integer"}, enforced=False))
    _, document = check_json("--only", "contract-diff")
    assert [(f["ruleId"], f["file"]) for f in document["findings"]] == [
        ("contract-diff/needs-base", "target/manifest.json")]


def test_contract_diff_against_a_base(workdir, check_json):
    needs("sqlglot")
    write_json(workdir / "base.json", manifest({"id": "bigint", "note": "varchar"}))
    write_json(workdir / "target/manifest.json", manifest({"id": "bigint", "note": "varchar(10)"}))
    code, document = check_json("--only", "contract-diff", "--base-manifest", "base.json")
    breaking = findings(document, "contract-diff/breaking")
    assert [f["subject"] for f in breaking] == ["model.shop.orders › note"]
    assert breaking[0]["file"] == "target/manifest.json"
    assert code == 1
    # The base manifest is never compared with itself.
    assert {f["file"] for f in document["findings"]} == {"target/manifest.json"}


def test_contract_diff_invalid_base(workdir, check_json):
    needs("sqlglot")
    write_json(workdir / "target/manifest.json", manifest({"id": "integer"}))
    write(workdir / "base.json", '{"nodes": []}')
    _, document = check_json("--only", "contract-diff", "--base-manifest", "base.json")
    assert rule_ids(document) == ["contract-diff/invalid-base"]


# --------------------------------------------------------------------------- dialect-audit

def test_dialect_audit_needs_a_dialect_pair(workdir, check_json):
    write(workdir / "q.sql", "select 1;\n")
    _, document = check_json("--only", "dialect-audit")
    (finding,) = document["findings"]
    assert finding["ruleId"] == "dialect-audit/needs-dialect" and finding["file"] is None
    assert "--dialect FROM:TO" in finding["hint"]


def test_dialect_audit_static_mode_skips_binding_noise(workdir, check_json):
    needs("sqlglot", "duckdb", "yaml")
    write(workdir / "report.sql", "SELECT 1;\nSELECT MY_UDF(amount) FROM orders;\n")
    write(workdir / "model.sql", "select * from {{ ref('x') }}\n")
    _, document = check_json("--only", "dialect-audit", "--dialect", "snowflake:duckdb")
    anonymous = findings(document, "dialect-audit/anonymous-function")
    assert [(f["file"], f["line"], f["subject"]) for f in anonymous] == [("report.sql", 2, "statement 2")]
    assert anonymous[0]["message"].startswith("my_udf: unknown to SQLGlot")
    assert not findings(document, "dialect-audit/unknown-table")  # no --sql-schema: binding is not reported
    assert findings(document, "dialect-audit/jinja-skipped")[0]["message"] == "Jinja in 1 SQL file: not audited"
    summary = findings(document, "dialect-audit/summary")[0]["message"]
    assert summary.startswith("snowflake -> duckdb: 2 statements in 1 file, 1 OK, 1 flagged")
    assert "DuckDB binding skipped" in summary


def test_dialect_audit_takes_the_source_dialect_from_the_manifest(workdir, check_json):
    needs("sqlglot", "duckdb", "yaml")
    write_json(workdir / "target/manifest.json", manifest({"id": "integer"}, adapter="snowflake"))
    write(workdir / "q.sql", "SELECT 1;\n")
    _, document = check_json("--only", "dialect-audit", "--dialect", ":duckdb")
    assert findings(document, "dialect-audit/summary")[0]["message"].startswith("snowflake -> duckdb: 1 statement")


def test_dialect_audit_unknown_dialect(workdir, check_json):
    needs("sqlglot", "duckdb", "yaml")
    write(workdir / "q.sql", "SELECT 1;\n")
    code, document = check_json("--only", "dialect-audit", "--dialect", "nosuchdialect:duckdb")
    assert rule_ids(document) == ["dialect-audit/invalid-config"] and code == 1


# --------------------------------------------------------------------------- lint-cache

def test_lint_cache_needs_a_sqlfluff_config(workdir, check_json):
    write(workdir / "q.sql", "select 1\n")
    _, document = check_json("--only", "lint-cache")
    assert rule_ids(document) == ["lint-cache/needs-config"]


def test_lint_cache_lints_without_writing_a_cache(workdir, check_json):
    needs("sqlfluff", "jinja2")
    write(workdir / ".sqlfluff", "[sqlfluff]\ndialect = ansi\ntemplater = raw\n")
    write(workdir / "q.sql", "SELECT 1\n")
    before = snapshot(workdir)
    _, document = check_json("--only", "lint-cache")
    (summary,) = findings(document, "lint-cache/summary")
    assert summary["file"] == ".sqlfluff" and summary["message"].startswith("1 SQL file linted")
    assert snapshot(workdir) == before


def test_lint_cache_never_runs_the_dbt_templater(workdir, check_json):
    needs("sqlfluff", "jinja2")
    write(workdir / ".sqlfluff", "[sqlfluff]\ndialect = duckdb\ntemplater = dbt\n")
    write(workdir / "models/q.sql", "select 1\n")
    _, document = check_json("--only", "lint-cache")
    ids = rule_ids(document)
    # Depending on the installed plugins SQLFluff refuses the templater itself or lint-cache does.
    assert ids in (["lint-cache/templater-not-run"], ["lint-cache/config-error"])


# --------------------------------------------------------------------------- seedtypes

def test_seedtypes_standalone_csv(workdir, check_json):
    needs("duckdb", "polars", "yaml")
    write(workdir / "exports/codes.csv", "code,label\n007,a\n042,b\n")
    code, document = check_json("--only", "seedtypes")
    (lossy,) = findings(document, "seedtypes/lossy-default")
    assert lossy["subject"] == "code" and lossy["severity"] == "info" and lossy["line"] == 2
    assert code == 0


def test_seedtypes_pinned_seed_is_clean(workdir, check_json):
    needs("duckdb", "polars", "yaml")
    write(workdir / "dbt_project.yml", "name: shop\nseeds:\n  shop:\n    codes:\n      +column_types:\n"
                                       "        code: varchar\n")
    write(workdir / "seeds/codes.csv", "code\n007\n042\n")
    _, document = check_json("--only", "seedtypes")
    assert rule_ids(document) == ["seedtypes/summary"]
    assert findings(document, "seedtypes/summary")[0]["message"].endswith("0 unpinned risks")


def test_seedtypes_jinja_in_project_config_is_a_notice(workdir, check_json):
    needs("duckdb", "polars", "yaml")
    write(workdir / "dbt_project.yml", "name: shop\nvars:\n  start: \"{{ env_var('START') }}\"\n")
    write(workdir / "seeds/codes.csv", "code\n007\n")
    _, document = check_json("--only", "seedtypes")
    (finding,) = findings(document, "seedtypes/not-checked")
    assert finding["severity"] == "info" and "Jinja" in finding["message"]


# --------------------------------------------------------------------------- parquet-unify

def test_parquet_drift(workdir, check_json):
    needs("pyarrow")
    import pyarrow as pa
    import pyarrow.parquet as pq

    folder = workdir / "lake/orders"
    folder.mkdir(parents=True)
    pq.write_table(pa.table({"id": pa.array([1], pa.int32()), "note": ["a"]}), folder / "a.parquet")
    pq.write_table(pa.table({"id": pa.array([2], pa.int64())}), folder / "b.parquet")
    pq.write_table(pa.table({"id": pa.array([3], pa.int64()), "note": [True]}), folder / "c.parquet")
    write(folder / "d.parquet", "truncated upload")
    code, document = check_json("--only", "parquet-unify")
    by_rule = {f["rule"]: f for f in document["findings"]}
    assert by_rule["widenable"]["subject"] == "/id" and "int64" in by_rule["widenable"]["message"]
    assert by_rule["type-conflict"]["subject"] == "/note" and by_rule["type-conflict"]["severity"] == "error"
    assert "missing in 1 of 3 files" in by_rule["type-conflict"]["message"]
    assert by_rule["unreadable"]["file"] == "lake/orders/d.parquet"
    assert by_rule["schema-groups"]["message"].startswith("3 files in 3 schema groups, 3 rows")
    assert code == 1


def test_parquet_consistent_dataset(workdir, check_json):
    needs("pyarrow")
    import pyarrow as pa
    import pyarrow.parquet as pq

    for day in ("2026-11-01", "2026-11-02"):
        folder = workdir / f"lake/events/dt={day}"
        folder.mkdir(parents=True)
        pq.write_table(pa.table({"id": [1, 2]}), folder / "part-0.parquet")
    _, document = check_json("--only", "parquet-unify")
    assert [(f["ruleId"], f["file"]) for f in document["findings"]] == [("parquet-unify/consistent", "lake/events")]


# --------------------------------------------------------------------------- duckmate

def test_duckmate_needs_duckdb_evidence(workdir, check_json):
    write(workdir / "db/migrations/001_init.sql", "-- migrate:up\nCREATE TABLE t (id SERIAL);\n-- migrate:down\n")
    _, document = check_json("--only", "duckmate")
    assert rule_ids(document) == ["duckmate/not-duckdb"]


def test_duckmate_parse_errors_and_invalid_files(workdir, check_json):
    needs("duckdb")
    write(workdir / "db/schema.sql", "-- duckmate schema-only snapshot v1 (sequence cursors and user rows omitted)\n")
    write(workdir / "db/migrations/001_init.sql",
          "-- migrate:up\nCREATE TABLE t (id INTEGER;\n-- migrate:down\nDROP TABLE t;\n")
    _, document = check_json("--only", "duckmate")
    (parse,) = findings(document, "duckmate/parse-error")
    assert parse["file"] == "db/migrations/001_init.sql" and parse["subject"] == "migrate:up"
    assert findings(document, "duckmate/summary")[0]["message"].startswith("1 migration (001): 1 of 2 sections")
    write(workdir / "db/migrations/002 bad name.sql", "-- migrate:up\nselect 1;\n-- migrate:down\n")
    _, document = check_json("--only", "duckmate")
    assert rule_ids(document) == ["duckmate/invalid-migration"]


# --------------------------------------------------------------------------- tapcheck

def test_tapcheck_clean_stream(workdir, check_json):
    needs("jsonschema")
    jsonl(workdir / "out.jsonl", [singer_schema(), singer_record(id=1, name="a"), SINGER_STATE])
    code, document = check_json("--only", "tapcheck")
    assert rule_ids(document) == ["tapcheck/summary"]
    assert document["findings"][0]["message"] == "1 record in 1 stream (singer); 0 contract problems"
    assert code == 0


def test_tapcheck_catalog_registration(workdir, check_json):
    needs("jsonschema")
    write_json(workdir / "catalog.json", {"streams": [
        {"tap_stream_id": "users", "stream": "users", "key_properties": ["id"],
         "schema": {"type": "object", "properties": {"id": {"type": "integer"}}}, "metadata": []}]})
    _, document = check_json("--only", "tapcheck")
    # The empty main input check uses internally must not surface as TC021.
    assert rule_ids(document) == ["tapcheck/catalog"]
    assert document["findings"][0]["message"].startswith("1 stream (singer)")


# --------------------------------------------------------------------------- singer-replay

def test_singer_replay_cassette(workdir, check_json):
    from datawrench.singer_replay.cassette import record
    from datawrench.singer_replay.common import encode

    schema = {"type": "SCHEMA", "stream": "users", "key_properties": ["id"], "replication_key": "id",
              "schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}
    messages = [schema, {"type": "RECORD", "stream": "users", "record": {"id": 1}}]
    record(io.BytesIO("".join(encode(m) + "\n" for m in messages).encode()), workdir / "cassette")
    _, document = check_json("--only", "singer-replay")
    (verified,) = findings(document, "singer-replay/verified")
    assert verified["file"] == "cassette" and "1 record in 1 stream" in verified["message"]
    manifest_path = workdir / "cassette/manifest.json"
    data = json.loads(manifest_path.read_text())
    data["message_count"] += 1
    manifest_path.write_text(json.dumps(data))
    code, document = check_json("--only", "singer-replay")
    assert rule_ids(document) == ["singer-replay/invalid-cassette"] and code == 1


# --------------------------------------------------------------------------- sqlmesh-manifest

def test_sqlmesh_project_without_export_gets_a_hint(workdir, check_json):
    write(workdir / "config.yaml", "model_defaults:\n  dialect: duckdb\n")
    write(workdir / "models/m.sql", "MODEL (name db.m);\nselect 1 as x\n")
    _, document = check_json("--only", "sqlmesh-manifest")
    (hint,) = document["findings"]
    assert hint["ruleId"] == "sqlmesh-manifest/not-exported"
    assert "datawrench sqlmesh-manifest export --project ." in hint["hint"]


def test_dbt_manifests_are_not_validated_by_sqlmesh_manifest(workdir, check_json):
    write_json(workdir / "target/manifest.json", manifest({"id": "integer"}))
    _, document = check_json("--only", "sqlmesh-manifest")
    assert document["findings"] == []


@pytest.mark.parametrize("name", ["contract-diff", "lint-cache", "dialect-audit"])
def test_checks_without_inputs_stay_silent(workdir, check_json, name):
    write(workdir / "README.md", "# nothing here\n")
    _, document = check_json("--only", name)
    assert document["findings"] == []
