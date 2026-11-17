import csv
import sqlite3

from sqlglot import exp

from datawrench.grain_contract import compile_contract

from .helpers import UPSTREAM, customer_contract, render_upstream


def test_real_jaffle_shop_cte_contracts_in_sqlite():
    con = sqlite3.connect(":memory:")
    try:
        for name in ("customers", "orders", "payments"):
            with (UPSTREAM / f"raw_{name}.csv").open(newline="") as stream:
                reader = csv.DictReader(stream)
                columns = reader.fieldnames
                quoted = ",".join(
                    exp.to_identifier(c).sql(dialect="sqlite", identify=True)
                    for c in columns
                )
                con.execute(f"CREATE TABLE raw_{name}({quoted})")
                for row in reader:
                    values = [
                        int(value) if value.isdigit() else value or None
                        for value in row.values()
                    ]
                    con.execute(
                        f"INSERT INTO raw_{name} VALUES({','.join('?' for _ in columns)})",
                        values,
                    )
            con.execute(f"CREATE VIEW stg_{name} AS {render_upstream('stg_' + name)}")
        c = customer_contract()
        c["dialect"] = "sqlite"
        bundle = compile_contract(render_upstream("customers"), c)
        assert all(not con.execute(check.sql).fetchall() for check in bundle.checks)
        con.execute("INSERT INTO raw_orders SELECT * FROM raw_orders WHERE id=1")
        bad = [check for check in bundle.checks if con.execute(check.sql).fetchall()]
        assert any(
            check.scope == "customer_payments" and check.kind == "row_count"
            for check in bad
        )
    finally:
        con.close()
