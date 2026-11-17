import pytest

from datawrench.grain_contract import ContractError, compile_contract

from .helpers import SIMPLE_SQL, connection, failures, simple_contract, simple_data


def test_full_join_can_declare_a_coalesced_grain():
    with connection() as con:
        con.execute("CREATE TABLE l(id INT); INSERT INTO l VALUES(1),(2)")
        con.execute("CREATE TABLE r(id INT); INSERT INTO r VALUES(2),(3)")
        c = {
            "version": 1,
            "primary_keys": {"l": ["id"], "r": ["id"]},
            "scopes": {
                "$root": {
                    "grain": ["a.id"],
                    "joins": {
                        "b": {
                            "cardinality": "1:1",
                            "preserve": "none",
                            "grain": ["COALESCE(a.id,b.id)"],
                        }
                    },
                }
            },
            "output_grain": ["id"],
        }
        sql = "SELECT COALESCE(a.id,b.id) AS id FROM l a FULL JOIN r b ON a.id=b.id"
        bundle = compile_contract(sql, c)
        assert not failures(con, bundle)
        assert bundle.joins[0].grain_after == ("COALESCE(a.id, b.id)",)
        con.execute("INSERT INTO r VALUES(3)")
        assert {
            check.kind for check, _ in failures(con, compile_contract(sql, c)).values()
        } == {"primary_key", "join_key", "grain", "output_grain"}


def test_scalar_grain_cast_and_expression_preservation():
    with connection() as con:
        simple_data(con)
        c = simple_contract()
        c["scopes"]["$root"]["grain"] = ["CAST(o.id AS VARCHAR)"]
        assert not failures(con, compile_contract(SIMPLE_SQL, c))
        con.execute("INSERT INTO customers VALUES(10,'Duplicate')")
        bad = failures(con, compile_contract(SIMPLE_SQL, c))
        assert "preservation" in {check.kind for check, _ in bad.values()}


def test_parenthesized_join_column_operands_are_supported():
    with connection() as con:
        simple_data(con)
        sql = SIMPLE_SQL.replace("o.customer_id = c.id", "((o.customer_id)) = (c.id)")
        assert not failures(con, compile_contract(sql, simple_contract()))


@pytest.mark.parametrize(
    "expression,match",
    [
        ("COUNT(o.id)", "aggregates"),
        ("ROW_NUMBER() OVER (ORDER BY o.id)", "windows"),
        ("(SELECT o.id)", "queries"),
        ("o.id AS key", "aliases"),
        ("o.*", "stars"),
        ("o.id + ?", "parameters"),
        ("o.id + RANDOM()", "deterministic"),
        ("o.id + CURRENT_TIMESTAMP", "deterministic"),
        ("id", "qualified"),
        ("1", "qualified"),
        ("o.id, o.customer_id", "one scalar"),
        ("o.id FROM orders o", "one scalar"),
        ("COALESCE(o.id,c.id)", "initial grain"),
    ],
)
def test_invalid_grain_expressions_fail_closed(expression, match):
    c = simple_contract()
    c["scopes"]["$root"]["grain"] = [expression]
    with pytest.raises(ContractError, match=match):
        compile_contract(SIMPLE_SQL, c)


def test_duplicate_expression_normalization():
    c = simple_contract()
    c["scopes"]["$root"]["grain"] = ["COALESCE(o.id,o.id)", 'COALESCE("O".id,o.id)']
    with pytest.raises(ContractError, match="duplicate grain expression"):
        compile_contract(SIMPLE_SQL, c)
