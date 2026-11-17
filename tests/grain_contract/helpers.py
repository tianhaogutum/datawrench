from pathlib import Path

import duckdb
from jinja2 import Environment, StrictUndefined

UPSTREAM = Path(__file__).parent / "fixtures" / "upstream"


def connection():
    return duckdb.connect(config={"threads": "2", "memory_limit": "128MB"})


def render_upstream(name):
    return (
        Environment(undefined=StrictUndefined)
        .from_string((UPSTREAM / f"{name}.sql").read_text())
        .render(ref=lambda name: name)
    )


def jaffle_shop(con):
    for name in ("customers", "orders", "payments"):
        path = str(UPSTREAM / f"raw_{name}.csv")
        con.execute(
            f"CREATE TABLE raw_{name} AS SELECT * FROM read_csv_auto(?)", [path]
        )
        con.execute(f"CREATE VIEW stg_{name} AS {render_upstream('stg_' + name)}")


def failures(con, bundle):
    result = {}
    for check in bundle.checks:
        rows = con.execute(check.sql).fetchall()
        if rows:
            result[check.id] = (check, rows)
    return result


def simple_contract():
    return {
        "version": 1,
        "dialect": "duckdb",
        "primary_keys": {"orders": ["id"], "customers": ["id"]},
        "scopes": {
            "$root": {
                "grain": ["o.id"],
                "joins": {"c": {"cardinality": "m:1", "preserve": "left"}},
            }
        },
        "output_grain": ["order_id"],
    }


SIMPLE_SQL = "SELECT o.id AS order_id, c.name FROM orders o LEFT JOIN customers c ON o.customer_id = c.id"


def simple_data(con):
    con.execute(
        "CREATE TABLE orders(id INT, customer_id INT); INSERT INTO orders VALUES (1,10),(2,99)"
    )
    con.execute(
        "CREATE TABLE customers(id INT, name VARCHAR); INSERT INTO customers VALUES (10,'Alice')"
    )


def customer_contract():
    return {
        "version": 1,
        "dialect": "duckdb",
        "primary_keys": {
            "customers": ["customer_id"],
            "orders": ["order_id"],
            "payments": ["payment_id"],
            "customer_orders": ["customer_id"],
            "customer_payments": ["customer_id"],
        },
        "scopes": {
            "customer_payments": {
                "grain": ["payments.payment_id"],
                "joins": {"orders": {"cardinality": "m:1", "preserve": "left"}},
            },
            "final": {
                "grain": ["customers.customer_id"],
                "joins": {
                    "customer_orders": {"cardinality": "m:1", "preserve": "left"},
                    "customer_payments": {"cardinality": "m:1", "preserve": "left"},
                },
            },
        },
        "output_grain": ["customer_id"],
    }
