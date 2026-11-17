import pytest

from datawrench.grain_contract import ContractError, compile_contract

from .helpers import SIMPLE_SQL, connection, failures, simple_contract, simple_data


def test_unused_ctes_are_pruned_but_transitive_dependencies_survive():
    with connection() as con:
        simple_data(con)
        sql = (
            "WITH unused AS (SELECT * FROM orders), a AS (SELECT * FROM customers), lookup AS (SELECT * FROM a) "
            + SIMPLE_SQL.replace("customers c", "lookup c")
        )
        c = simple_contract()
        c["primary_keys"]["lookup"] = c["primary_keys"].pop("customers")
        bundle = compile_contract(sql, c)
        assert not failures(con, bundle)
        assert all("unused AS" not in check.sql for check in bundle.checks)
        lookup_check = next(
            check for check in bundle.checks if check.kind == "join_key"
        )
        assert "a AS" in lookup_check.sql and "lookup AS" in lookup_check.sql


def test_unsupported_join_cannot_hide_in_an_unused_cte():
    sql = "WITH unused AS (SELECT * FROM orders a CROSS JOIN customers b) " + SIMPLE_SQL
    with pytest.raises(ContractError, match="scope unused"):
        compile_contract(sql, simple_contract())


@pytest.mark.parametrize(
    "prefix",
    [
        "WITH written AS (DELETE FROM orders RETURNING id) ",
        "WITH written AS (MERGE INTO orders a USING customers b ON a.id=b.id WHEN MATCHED THEN UPDATE SET customer_id=b.id) ",
    ],
)
def test_data_modifying_ctes_are_rejected(prefix):
    c = simple_contract()
    c["dialect"] = "postgres"
    with pytest.raises(ContractError, match="read-only"):
        compile_contract(prefix + SIMPLE_SQL, c)


def test_select_for_update_is_not_a_read_only_assertion():
    c = simple_contract()
    c["dialect"] = "postgres"
    with pytest.raises(ContractError, match="read-only"):
        compile_contract(SIMPLE_SQL + " FOR UPDATE", c)
