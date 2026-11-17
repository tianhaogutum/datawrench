import copy
import hashlib
import json

import pytest

from datawrench.grain_contract import ContractError, compile_contract

from .helpers import (
    SIMPLE_SQL,
    UPSTREAM,
    connection,
    customer_contract,
    failures,
    jaffle_shop,
    render_upstream,
    simple_contract,
    simple_data,
)


@pytest.fixture
def con():
    db = connection()
    try:
        yield db
    finally:
        db.close()


def test_clean_left_join_and_nullable_foreign_key(con):
    simple_data(con)
    con.execute("INSERT INTO orders VALUES(3,NULL)")
    bundle = compile_contract(SIMPLE_SQL, simple_contract())
    assert len(bundle.checks) == 9
    assert failures(con, bundle) == {}


def test_dirty_dimension_is_localized(con):
    simple_data(con)
    con.execute("INSERT INTO customers VALUES(10,'Duplicate')")
    bad = failures(con, compile_contract(SIMPLE_SQL, simple_contract()))
    assert {c.kind for c, _ in bad.values()} == {
        "primary_key",
        "join_key",
        "join_cardinality",
        "row_count",
        "preservation",
        "grain",
        "output_grain",
    }
    assert next(rows for c, rows in bad.values() if c.kind == "preservation") == [
        (1, 1, 2)
    ]


def test_inner_join_row_loss_and_explicit_opt_out(con):
    simple_data(con)
    sql = SIMPLE_SQL.replace("LEFT JOIN", "INNER JOIN")
    contract = simple_contract()
    bad = failures(con, compile_contract(sql, contract))
    assert {c.kind for c, _ in bad.values()} == {"row_count", "preservation"}
    contract["scopes"]["$root"]["joins"]["c"]["preserve"] = "none"
    assert not failures(con, compile_contract(sql, contract))


def test_equal_total_cannot_hide_lost_and_multiplied_keys(con):
    simple_data(con)
    con.execute("INSERT INTO customers VALUES (10,'Duplicate')")
    sql = SIMPLE_SQL.replace("LEFT JOIN", "INNER JOIN")
    bad = failures(con, compile_contract(sql, simple_contract()))
    assert "row_count" not in {c.kind for c, _ in bad.values()}
    rows = next(rows for c, rows in bad.values() if c.kind == "preservation")
    assert set(rows) == {(1, 1, 2), (2, 1, 0)}


def test_where_and_distinct_cannot_hide_a_join_explosion(con):
    simple_data(con)
    con.execute("INSERT INTO customers VALUES (10,'Duplicate')")
    sql = (
        SIMPLE_SQL.replace("SELECT o.id", "SELECT DISTINCT o.id")
        + " WHERE c.name='Alice'"
    )
    bad = failures(con, compile_contract(sql, simple_contract()))
    assert "row_count" in {c.kind for c, _ in bad.values()}
    assert "output_grain" not in {c.kind for c, _ in bad.values()}


def test_join_keys_are_not_assumed_to_be_primary_keys(con):
    simple_data(con)
    con.execute("INSERT INTO customers VALUES (11,'Alice')")
    sql = SIMPLE_SQL.replace("o.customer_id = c.id", "o.customer_id = c.id")
    # Use a different, nonunique join key while declared PK id remains clean.
    con.execute("ALTER TABLE customers ADD COLUMN legacy_id INT DEFAULT 10")
    sql = sql.replace("c.id", "c.legacy_id")
    bad = failures(con, compile_contract(sql, simple_contract()))
    assert "primary_key" not in {c.kind for c, _ in bad.values()}
    assert "join_key" in {c.kind for c, _ in bad.values()}


def test_composite_keys_swapped_predicates_and_null_semantics(con):
    con.execute("CREATE TABLE l(tenant INT,id INT); INSERT INTO l VALUES(1,1),(2,1)")
    con.execute(
        "CREATE TABLE r(pk INT, tenant INT,id INT); INSERT INTO r VALUES(1,1,1),(2,2,1),(3,NULL,1),(4,NULL,1)"
    )
    c = {
        "version": 1,
        "primary_keys": {"l": ["tenant", "id"], "r": ["pk"]},
        "scopes": {
            "$root": {
                "grain": ["a.tenant", "a.id"],
                "joins": {"b": {"cardinality": "1:1", "preserve": "left"}},
            }
        },
        "output_grain": ["tenant", "id"],
    }
    sql = "SELECT a.tenant,a.id FROM l a LEFT JOIN r b ON (b.tenant=a.tenant AND a.id=b.id)"
    assert not failures(con, compile_contract(sql, c))


@pytest.mark.parametrize("kind", ["INNER", "RIGHT", "FULL"])
def test_right_preservation_and_new_grain(con, kind):
    simple_data(con)
    con.execute("DELETE FROM orders WHERE customer_id=99")
    c = simple_contract()
    c["scopes"]["$root"]["joins"]["c"] = {"cardinality": "1:m", "preserve": "right"}
    c["output_grain"] = ["customer_id"]
    sql = f"SELECT c.id AS customer_id FROM orders o {kind} JOIN customers c ON o.customer_id=c.id"
    assert not failures(con, compile_contract(sql, c))


def test_one_to_many_explicit_combined_grain(con):
    simple_data(con)
    con.execute("INSERT INTO customers VALUES(11,'Alice')")
    c = simple_contract()
    c["scopes"]["$root"]["joins"]["c"] = {
        "cardinality": "1:m",
        "preserve": "none",
        "grain": ["o.id", "c.id"],
    }
    c["output_grain"] = ["order_id", "customer_id"]
    sql = "SELECT o.id order_id, c.id customer_id FROM orders o JOIN customers c ON o.customer_id < c.id"
    with pytest.raises(ContractError, match="conjunction"):
        compile_contract(sql, c)
    sql = sql.replace("o.customer_id < c.id", "o.customer_id = c.id")
    assert not failures(con, compile_contract(sql, c))


def test_real_jaffle_shop_models(con):
    jaffle_shop(con)
    assert con.execute("SELECT COUNT(*) FROM raw_customers").fetchone()[0] == 100
    assert con.execute("SELECT COUNT(*) FROM raw_orders").fetchone()[0] == 99
    customers = compile_contract(render_upstream("customers"), customer_contract())
    assert not failures(con, customers)
    orders_contract = {
        "version": 1,
        "primary_keys": {"orders": ["order_id"], "order_payments": ["order_id"]},
        "scopes": {
            "final": {
                "grain": ["orders.order_id"],
                "joins": {"order_payments": {"cardinality": "m:1", "preserve": "left"}},
            }
        },
        "output_grain": ["order_id"],
    }
    assert not failures(
        con, compile_contract(render_upstream("orders"), orders_contract)
    )
    # A real seed regression, hidden by the upstream customer_payments GROUP BY.
    con.execute("INSERT INTO raw_orders SELECT * FROM raw_orders WHERE id=1")
    bad = failures(
        con, compile_contract(render_upstream("customers"), customer_contract())
    )
    assert any(
        c.scope == "customer_payments" and c.kind == "row_count"
        for c, _ in bad.values()
    )


def test_upstream_fixtures_have_exact_recorded_hashes():
    provenance = json.loads((UPSTREAM / "provenance.json").read_text())
    for record in provenance["files"]:
        assert (
            hashlib.sha256((UPSTREAM / record["file"]).read_bytes()).hexdigest()
            == record["sha256"]
        )


@pytest.mark.parametrize(
    "sql,match",
    [
        ("SELECT 1; SELECT 2", "exactly one"),
        ("DELETE FROM orders", "one SELECT"),
        (
            SIMPLE_SQL.replace("o.customer_id = c.id", "o.customer_id > c.id"),
            "conjunction",
        ),
        (SIMPLE_SQL.replace("o.customer_id = c.id", "customer_id = c.id"), "qualified"),
        (
            SIMPLE_SQL.replace(
                "o.customer_id = c.id", "o.customer_id = c.id OR o.id=c.id"
            ),
            "conjunction",
        ),
        (SIMPLE_SQL.replace("ON o.customer_id = c.id", "USING (id)"), "explicit equi"),
        (SIMPLE_SQL.replace("LEFT JOIN", "CROSS JOIN"), "explicit equi"),
        (SIMPLE_SQL.replace("customers c", "(SELECT * FROM customers) c"), "use a CTE"),
        (SIMPLE_SQL.replace("o.customer_id = c.id", "o.id = o.customer_id"), "connect"),
        (SIMPLE_SQL.replace("o.customer_id = c.id", "z.id = c.id"), "connect"),
    ],
)
def test_unsupported_queries_fail_closed(sql, match):
    with pytest.raises(ContractError, match=match):
        compile_contract(sql, simple_contract())


def test_missing_or_stale_scope_and_join_declarations():
    c = simple_contract()
    c["scopes"] = {}
    with pytest.raises(ContractError, match="needs a scope"):
        compile_contract(SIMPLE_SQL, c)
    c = simple_contract()
    c["scopes"]["$root"]["joins"] = {
        "old_alias": {"cardinality": "m:1", "preserve": "left"}
    }
    with pytest.raises(ContractError, match="exactly match"):
        compile_contract(SIMPLE_SQL, c)


@pytest.mark.parametrize(
    "path,value",
    [
        ("version", True),
        ("output_grain", []),
        ("primary_keys", {}),
        ("dialect", "madeup"),
        ("unexpected", 1),
    ],
)
def test_invalid_declarations(path, value):
    c = copy.deepcopy(simple_contract())
    c[path] = value
    with pytest.raises(ContractError):
        compile_contract(SIMPLE_SQL, c)
