from datawrench.grain_contract import compile_contract

from .helpers import connection, failures, simple_contract


def test_implicit_cast_can_make_distinct_right_keys_fan_out():
    with connection() as con:
        con.execute(
            "CREATE TABLE orders(id INT,customer_id INT); INSERT INTO orders VALUES(1,1)"
        )
        con.execute(
            "CREATE TABLE customers(id VARCHAR); INSERT INTO customers VALUES('1'),('01')"
        )
        c = simple_contract()
        rule = c["scopes"]["$root"]["joins"]["c"]
        rule.update(preserve="none", grain=["o.id", "c.id"])
        c["output_grain"] = ["order_id", "customer_id"]
        sql = "SELECT o.id AS order_id,c.id AS customer_id FROM orders o JOIN customers c ON o.customer_id=c.id"
        assert len(con.execute(sql).fetchall()) == 2
        bad = failures(con, compile_contract(sql, c))
        assert [(check.kind, rows) for check, rows in bad.values()] == [
            ("join_cardinality", [(1, 2)])
        ]


def test_cardinality_matches_right_rows_under_implicit_cast():
    with connection() as con:
        con.execute(
            "CREATE TABLE orders(id VARCHAR,customer_id VARCHAR); INSERT INTO orders VALUES('1','1'),('2','01')"
        )
        con.execute("CREATE TABLE customers(id INT); INSERT INTO customers VALUES(1)")
        c = simple_contract()
        rule = c["scopes"]["$root"]["joins"]["c"]
        rule.update(cardinality="1:m", preserve="none", grain=["o.id", "c.id"])
        c["output_grain"] = ["order_id", "customer_id"]
        sql = "SELECT o.id AS order_id,c.id AS customer_id FROM orders o JOIN customers c ON o.customer_id=c.id"
        bad = failures(con, compile_contract(sql, c))
        assert [(check.kind, rows) for check, rows in bad.values()] == [
            ("join_cardinality", [(1, 2)])
        ]
