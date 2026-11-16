"""Input discovery: recognised by content, read-only, no symlinks, no build output."""
import os

import pytest

from datawrench.check.discover import DiscoveryError, discover, is_catalog, manifest_meta, message_protocol

from .conftest import SINGER_STATE, jsonl, manifest, singer_record, singer_schema, write, write_json


def kinds(discovery):
    return {source.display: source.kind for source in discovery.sources}


def test_python_files_count_only_when_they_import_polars(workdir):
    write(workdir / "etl/a.py", "import polars as pl\n")
    write(workdir / "etl/b.py", "from polars import col\n")
    write(workdir / "etl/c.py", "import pandas as pd\n# import polars is only a comment here\n")
    assert kinds(discover(["."])) == {"etl/a.py": "python-polars", "etl/b.py": "python-polars"}


def test_sql_migrations_and_sqlmesh_models_are_told_apart(workdir):
    write(workdir / "queries/report.sql", "select 1;\n")
    write(workdir / "models/orders.sql", "select * from {{ ref('stg') }}\n")
    write(workdir / "db/migrations/001_init.sql", "-- migrate:up\ncreate table t (id int);\n-- migrate:down\n")
    write(workdir / "db/migrations/002_more.sql", "-- migrate:up\nselect 1;\n-- migrate:down\n")
    write(workdir / "db/schema.sql", "-- duckmate schema-only snapshot v1 (sequence cursors and user rows omitted)\n")
    write(workdir / "mesh/models/m.sql", "-- header\nMODEL (name db.m, kind FULL);\nselect 1 as x\n")
    found = discover(["."])
    assert kinds(found) == {"queries/report.sql": "sql", "models/orders.sql": "sql",
                            "db/migrations": "duckmate-migrations"}
    jinja = {source.display: source.meta["jinja"] for source in found.sources if source.kind == "sql"}
    assert jinja == {"queries/report.sql": False, "models/orders.sql": True}


def test_csv_inside_a_dbt_project_belongs_to_the_project(workdir):
    write(workdir / "dbt_project.yml", "name: shop\n")
    write(workdir / "seeds/codes.csv", "code\n007\n")
    write(workdir / "../elsewhere.csv", "a\n1\n")  # outside the scan
    write(workdir / "exports/report.csv", "a\n1\n")
    assert kinds(discover(["."])) == {".": "dbt-project", "exports/report.csv": "csv"}


def test_dbt_target_is_read_but_not_its_subfolders(workdir):
    write_json(workdir / "target/manifest.json", manifest({"id": "integer"}))
    write(workdir / "target/compiled/shop/models/orders.sql", "select 1\n")
    write(workdir / "target/run/shop/models/orders.sql", "create table x as select 1\n")
    found = discover(["."])
    assert kinds(found) == {"target/manifest.json": "dbt-manifest"}
    meta = found.sources[0].meta
    assert meta["adapter_type"] == "duckdb" and meta["enforced"] == 1 and meta["models"] == 1
    # Passing the compiled folder explicitly audits it.
    assert kinds(discover(["target/compiled"])) == {"target/compiled/shop/models/orders.sql": "sql"}


def test_ignored_folders_and_symlinks(workdir):
    write(workdir / ".venv/lib/x.py", "import polars\n")
    write(workdir / "node_modules/y.sql", "select 1\n")
    write(workdir / "dbt_packages/pkg/z.sql", "select 1\n")
    real = write(workdir / "real/q.sql", "select 1\n")
    os.symlink(real.parent, workdir / "linked")
    os.symlink(real, workdir / "q-link.sql")
    assert kinds(discover(["."])) == {"real/q.sql": "sql"}


def test_parquet_files_group_into_datasets_above_hive_partitions(workdir):
    for name in ("lake/orders/dt=2026-11-01/part-0.parquet", "lake/orders/dt=2026-11-02/part-0.parquet",
                 "lake/customers/a.parquet", "lake/customers/b.parquet"):
        write(workdir / name, "not read during discovery")
    found = discover(["."])
    datasets = {source.display: [display for _, display in source.files] for source in found.sources}
    assert datasets == {
        "lake/customers": ["lake/customers/a.parquet", "lake/customers/b.parquet"],
        "lake/orders": ["lake/orders/dt=2026-11-01/part-0.parquet", "lake/orders/dt=2026-11-02/part-0.parquet"],
    }


def test_singer_output_catalogs_and_cassettes(workdir):
    jsonl(workdir / "taps/users.jsonl", [singer_schema(), singer_record(id=1), SINGER_STATE])
    jsonl(workdir / "taps/redirected.json", [singer_schema(), singer_record(id=1)])
    write(workdir / "taps/notes.jsonl", '{"hello": "world"}\n')
    write_json(workdir / "taps/catalog.json", {"streams": [
        {"tap_stream_id": "users", "stream": "users", "schema": {"type": "object", "properties": {}}}]})
    for name in ("manifest.json", "catalog.json"):
        write_json(workdir / "cassette" / name, {"format_version": 1})
    write(workdir / "cassette/messages.jsonl.gz", "")
    assert kinds(discover(["."])) == {
        "taps/users.jsonl": "singer-messages", "taps/redirected.json": "singer-messages",
        "taps/catalog.json": "singer-catalog", "cassette": "singer-cassette",
    }


def test_sqlfluff_configs(workdir):
    write(workdir / ".sqlfluff", "[sqlfluff]\ndialect = duckdb\n")
    write(workdir / "sub/pyproject.toml", "[tool.sqlfluff.core]\ndialect = ansi\n")
    write(workdir / "other/pyproject.toml", "[project]\nname = 'x'\n")
    write(workdir / "other/setup.cfg", "[sqlfluff:rules]\ntab_space_size = 4\n")
    assert kinds(discover(["."])) == {".sqlfluff": "sqlfluff-config", "sub/pyproject.toml": "sqlfluff-config",
                                      "other/setup.cfg": "sqlfluff-config"}


def test_sqlmesh_project_detection(workdir):
    write(workdir / "mesh/config.yaml", "gateways:\n  local:\n    connection:\n      type: duckdb\n")
    write(workdir / "mesh/models/m.sql", "MODEL (name db.m);\nselect 1\n")
    write(workdir / "app/config.yaml", "debug: true\n")
    write(workdir / "app/models/user.py", "class User: pass\n")
    assert kinds(discover(["."])) == {"mesh": "sqlmesh-project"}


def test_broken_announced_json_is_reported_others_are_skipped(workdir):
    write(workdir / "target/manifest.json", '{"metadata": ')
    write(workdir / "package.json", "{not json")
    found = discover(["."])
    assert found.sources == []
    assert [(f.rule_id, f.file) for f in found.findings] == [("parse/invalid-json", "target/manifest.json")]


def test_explicit_files_that_are_not_inputs_are_reported(workdir):
    write(workdir / "notes.txt", "hello")
    write(workdir / "plain.py", "print(1)\n")
    found = discover(["notes.txt", "plain.py"])
    assert sorted((f.rule_id, f.file) for f in found.findings) == [
        ("parse/unrecognised", "notes.txt"), ("parse/unrecognised", "plain.py")]


@pytest.mark.parametrize("arg, message", [("", "empty path"), ("nope", "no such file or directory")])
def test_unusable_arguments(workdir, arg, message):
    with pytest.raises(DiscoveryError, match=message):
        discover([arg])


def test_classifiers():
    assert manifest_meta(manifest({"id": "int"}, enforced=False))["enforced"] == 0
    assert manifest_meta({"metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/catalog/v1.json"},
                          "nodes": {}}) is None
    assert is_catalog({"streams": [{"stream": {"name": "users", "json_schema": {}}}]})
    assert not is_catalog({"streams": []})
    assert message_protocol(b'{"type":"RECORD","stream":"s","record":{}}\n') == "singer"
    assert message_protocol(b'{"type":"RECORD","record":{"stream":"s","data":{},"emitted_at":1}}\n') == "airbyte"
    assert message_protocol(b"not json\n") is None
