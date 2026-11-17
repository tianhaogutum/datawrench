import copy
import json
from pathlib import Path

import pytest

from datawrench.grain_contract import ContractError, compile_contract
from datawrench.grain_contract.cli import main
from datawrench.grain_contract.dbt import from_manifest

FIXTURE = Path(__file__).parent / "fixtures/dbt/manifest.json"


@pytest.fixture
def manifest():
    return json.loads(FIXTURE.read_text())


def direct_contract():
    return {
        "version": 1,
        "dialect": "duckdb",
        "primary_keys": {
            "ref:stg_orders": ["order_id"],
            "ref:stg_customers": ["customer_id"],
        },
        "scopes": {
            "$root": {
                "grain": ["o.order_id"],
                "joins": {"c": {"cardinality": "m:1", "preserve": "left"}},
            }
        },
        "output_grain": ["order_id"],
    }


def test_real_compiled_manifest_resolves_relation_names(manifest):
    original = copy.deepcopy(direct_contract())
    adapted = from_manifest(manifest, "order_enriched", original)
    assert original == direct_contract()
    assert all("ref:" not in key for key in adapted.contract["primary_keys"])
    assert len(compile_contract(adapted.sql, adapted.contract).checks) == 9
    assert adapted.provenance["dbt_version"] == "1.12.6"
    assert adapted.provenance["resolved_primary_keys"][0]["unique_id"].startswith(
        "model."
    )


def test_legacy_compiled_sql_field_adapter(manifest):
    # Structural compatibility test only; this fixture was produced by dbt 1.12.
    node = manifest["nodes"]["model.grain_contract_demo.order_enriched"]
    node["compiled_sql"] = node.pop("compiled_code")
    assert (
        from_manifest(manifest, "order_enriched", direct_contract()).sql
        == node["compiled_sql"]
    )


def test_missing_compiled_code_never_uses_raw_jinja(manifest):
    node = manifest["nodes"]["model.grain_contract_demo.order_enriched"]
    node["raw_code"] = node.pop("compiled_code")
    with pytest.raises(ContractError, match="run dbt compile"):
        from_manifest(manifest, "order_enriched", direct_contract())


def test_missing_source_checksum_is_rejected(manifest):
    del manifest["nodes"]["model.grain_contract_demo.order_enriched"]["checksum"]
    with pytest.raises(ContractError, match="source checksum"):
        from_manifest(manifest, "order_enriched", direct_contract())


@pytest.mark.parametrize("field", ["dbt_version", "dbt_schema_version"])
def test_bad_metadata_types_are_rejected_before_export(
    manifest, field, tmp_path, capsys
):
    manifest["metadata"][field] = 1.5
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(direct_contract()))
    out = tmp_path / "generated"
    assert (
        main(
            [
                "from-dbt",
                "--manifest",
                str(path),
                "--model",
                "order_enriched",
                "--contract",
                str(contract),
                "--out",
                str(out),
            ]
        )
        == 2
    )
    assert not out.exists()
    assert "must be a string" in capsys.readouterr().err


def test_ambiguous_names_need_unique_id(manifest):
    node = copy.deepcopy(manifest["nodes"]["model.grain_contract_demo.stg_orders"])
    node["unique_id"] = "model.other.stg_orders"
    manifest["nodes"][node["unique_id"]] = node
    with pytest.raises(ContractError, match="ambiguous"):
        from_manifest(manifest, "order_enriched", direct_contract())
    c = direct_contract()
    c["primary_keys"]["ref:model.grain_contract_demo.stg_orders"] = c[
        "primary_keys"
    ].pop("ref:stg_orders")
    assert from_manifest(manifest, "order_enriched", c)


def test_source_primary_key_resolution(manifest):
    manifest["sources"] = {
        "source.example.raw.orders": {
            "resource_type": "source",
            "name": "orders",
            "unique_id": "source.example.raw.orders",
            "relation_name": '"warehouse"."raw"."orders"',
        }
    }
    c = direct_contract()
    c["primary_keys"] = {"source:source.example.raw.orders": ["id"]}
    assert from_manifest(manifest, "order_enriched", c).contract["primary_keys"] == {
        '"warehouse"."raw"."orders"': ["id"]
    }


def test_from_dbt_cli_exports_package_ref_and_provenance(manifest, tmp_path, capsys):
    c = tmp_path / "contract.json"
    c.write_text(json.dumps(direct_contract()))
    out = tmp_path / "generated"
    args = [
        "from-dbt",
        "--manifest",
        str(FIXTURE),
        "--model",
        "order_enriched",
        "--contract",
        str(c),
        "--out",
        str(out),
    ]
    assert main(args) == 0
    assert len(list(out.glob("*.sql"))) == 9
    m = json.loads((out / "manifest.json").read_text())
    assert m["provenance"]["kind"] == "dbt_manifest"
    sql = next(out.glob("*.sql")).read_text()
    assert "ref('grain_contract_demo', 'order_enriched')" in sql
    assert not sql.rstrip().endswith(";")
    assert "Stale SQL grain contract" in sql
    assert main(args + ["--check"]) == 0
    original = next(out.glob("*.sql"))
    original.write_text(original.read_text() + "-- edited\n")
    assert main(args + ["--check"]) == 1
    assert main(args + ["--replace"]) == 0
    assert main(args + ["--check"]) == 0


def test_changed_compiled_sql_or_contract_makes_ci_check_stale(manifest, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(direct_contract()))
    out = tmp_path / "generated"
    args = [
        "from-dbt",
        "--manifest",
        str(path),
        "--model",
        "order_enriched",
        "--contract",
        str(contract),
        "--out",
        str(out),
    ]
    assert main(args) == 0
    node = manifest["nodes"]["model.grain_contract_demo.order_enriched"]
    node["compiled_code"] += (
        "\n-- SQL changed through a macro, while raw model checksum stays the same\n"
    )
    path.write_text(json.dumps(manifest))
    assert main(args + ["--check"]) == 1
