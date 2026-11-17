import copy
import sqlite3

import pytest
import sqlglot

from datawrench.grain_contract import ContractError, compile_contract

from .helpers import SIMPLE_SQL, connection, failures, simple_contract, simple_data


def test_unnecessary_quoting_and_case_do_not_change_relation_identity():
    with connection() as con:
        simple_data(con)
        sql = SIMPLE_SQL.replace("orders o", '"Orders" AS "O"').replace(
            "customers c", '"customers" c'
        )
        assert not failures(con, compile_contract(sql, simple_contract()))


def test_quoted_spaces_and_reserved_names():
    with connection() as con:
        con.execute(
            'CREATE TABLE "order data"("order id" INT,"ref" INT); INSERT INTO "order data" VALUES(1,2)'
        )
        con.execute(
            'CREATE TABLE "lookup"("primary key" INT); INSERT INTO "lookup" VALUES(2)'
        )
        c = {
            "version": 1,
            "primary_keys": {
                '"order data"': ['"order id"'],
                "lookup": ['"primary key"'],
            },
            "scopes": {
                "$root": {
                    "grain": ['"left side"."order id"'],
                    "joins": {
                        '"right side"': {"cardinality": "m:1", "preserve": "left"}
                    },
                }
            },
            "output_grain": ['"order id"'],
        }
        sql = 'SELECT "left side"."order id" FROM "order data" "left side" LEFT JOIN lookup "right side" ON "left side".ref="right side"."primary key"'
        assert not failures(con, compile_contract(sql, c))


def test_duplicate_normalized_columns_are_rejected():
    c = simple_contract()
    c["primary_keys"]["orders"] = ["id", '"ID"']
    with pytest.raises(ContractError, match="duplicate columns after dialect"):
        compile_contract(SIMPLE_SQL, c)


@pytest.mark.parametrize(
    "dialect", ["duckdb", "sqlite", "postgres", "snowflake", "bigquery"]
)
def test_emitted_queries_reparse_in_their_own_dialect(dialect):
    c = simple_contract()
    c["dialect"] = dialect
    bundle = compile_contract(SIMPLE_SQL, c)
    for check in bundle.checks:
        assert isinstance(
            sqlglot.parse_one(check.sql, read=dialect), sqlglot.exp.Select
        )


def test_sqlite_execution_matches_duckdb():
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(
            "CREATE TABLE orders(id INT,customer_id INT); INSERT INTO orders VALUES(1,10),(2,99); CREATE TABLE customers(id INT,name TEXT); INSERT INTO customers VALUES(10,'Alice')"
        )
        c = simple_contract()
        c["dialect"] = "sqlite"
        bundle = compile_contract(SIMPLE_SQL, c)
        assert all(con.execute(check.sql).fetchall() == [] for check in bundle.checks)
        con.execute("INSERT INTO customers VALUES(10,'Duplicate')")
        bad = {
            check.kind for check in bundle.checks if con.execute(check.sql).fetchall()
        }
        assert {"join_key", "row_count", "preservation", "grain", "output_grain"} <= bad
    finally:
        con.close()


@pytest.mark.parametrize(
    "field,value",
    [("cardinality", []), ("cardinality", {}), ("preserve", []), ("preserve", {})],
)
def test_bad_rule_types_raise_contract_error(field, value):
    c = copy.deepcopy(simple_contract())
    c["scopes"]["$root"]["joins"]["c"][field] = value
    with pytest.raises(ContractError, match=field):
        compile_contract(SIMPLE_SQL, c)
