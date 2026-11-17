from pathlib import Path

import pytest

from datawrench.grain_contract import ContractError, compile_contract
from datawrench.grain_contract.cli import _test_files
from datawrench.grain_contract.dbt import from_manifest, read_manifest

from .helpers import (
    SIMPLE_SQL,
    connection,
    customer_contract,
    failures,
    simple_contract,
    simple_data,
)
from .test_dbt import direct_contract

FIXTURE = Path(__file__).parent / "fixtures/dbt_ephemeral/manifest.json"


def test_real_ephemeral_ref_names_and_nested_ctes():
    adapted = from_manifest(
        read_manifest(FIXTURE, "order_enriched", direct_contract()),
        "order_enriched",
        direct_contract(),
    )
    assert set(adapted.contract["primary_keys"]) == {
        "__dbt__cte__stg_orders",
        "__dbt__cte__stg_customers",
    }
    assert len(adapted.provenance["ephemeral_checksums"]) == 2
    bundle = compile_contract(adapted.sql, adapted.contract)
    assert len(bundle.checks) == 9
    sql = next(
        iter(
            _test_files(
                bundle, adapted.model, adapted.package, adapted.provenance
            ).values()
        )
    )
    assert "model.grain_contract_demo.stg_orders" in sql
    assert "model.grain_contract_demo.stg_customers" in sql


def test_selective_reader_loads_injected_dependencies_without_ref_keys():
    c = customer_contract()
    selected = read_manifest(FIXTURE, "customers", c)
    assert len(selected["nodes"]) == 4  # consuming model + three injected staging nodes
    adapted = from_manifest(selected, "customers", c)
    assert len(compile_contract(adapted.sql, adapted.contract).checks) == 23
    assert len(adapted.provenance["ephemeral_checksums"]) == 3


def test_missing_injected_node_cannot_disable_stale_guard():
    selected = read_manifest(FIXTURE, "customers", customer_contract())
    del selected["nodes"]["model.grain_contract_demo.stg_orders"]
    with pytest.raises(ContractError, match="absent from the manifest"):
        from_manifest(selected, "customers", customer_contract())


def test_transitive_ephemeral_sources_are_selected_and_guarded():
    path = FIXTURE.parent.parent / "dbt_ephemeralchain/manifest.json"
    selected = read_manifest(path, "customers", customer_contract())
    assert len(selected["nodes"]) == 5
    adapted = from_manifest(selected, "customers", customer_contract())
    guards = {g["unique_id"] for g in adapted.provenance["ephemeral_checksums"]}
    assert len(guards) == 4 and "model.grain_contract_demo.base_orders" in guards
    assert len(compile_contract(adapted.sql, adapted.contract).checks) == 23


def test_opaque_nested_cte_keeps_its_local_scope():
    with connection() as con:
        simple_data(con)
        sql = (
            "WITH lookup AS (WITH stage AS (SELECT * FROM customers) SELECT * FROM stage) "
            + SIMPLE_SQL.replace("customers c", "lookup c")
        )
        c = simple_contract()
        c["primary_keys"]["lookup"] = c["primary_keys"].pop("customers")
        assert not failures(con, compile_contract(sql, c))


def test_contracting_a_join_scope_with_local_with_still_fails_closed():
    sql = (
        "WITH lookup AS (WITH stage AS (SELECT * FROM customers) SELECT a.id FROM stage a JOIN orders b ON a.id=b.customer_id) "
        + SIMPLE_SQL.replace("customers c", "lookup c")
    )
    c = simple_contract()
    c["primary_keys"]["lookup"] = ["id"]
    c["scopes"]["lookup"] = {
        "grain": ["a.id"],
        "joins": {
            "b": {"cardinality": "1:m", "preserve": "none", "grain": ["a.id", "b.id"]}
        },
    }
    with pytest.raises(ContractError, match="cannot own a nested WITH"):
        compile_contract(sql, c)
