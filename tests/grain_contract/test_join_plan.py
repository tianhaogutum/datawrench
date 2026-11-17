from datawrench.grain_contract import compile_contract

from .helpers import SIMPLE_SQL, connection, failures, simple_contract, simple_data


def test_manifest_includes_reviewable_join_plan_and_key_labels():
    bundle = compile_contract(SIMPLE_SQL, simple_contract())
    plan = bundle.joins[0]
    assert (plan.scope, plan.position, plan.join_type) == ("$root", 1, "LEFT")
    assert plan.left_aliases == ("o",)
    assert plan.right_alias == "c"
    assert plan.predicate == "o.customer_id = c.id"
    assert plan.left_keys == ("o.customer_id",) and plan.right_keys == ("c.id",)
    assert plan.grain_before == plan.grain_after == ("o.id",)
    assert next(c for c in bundle.checks if c.kind == "preservation").key_columns == (
        "o.id",
    )


def test_second_join_validates_actual_left_prefix_not_base_table():
    with connection() as con:
        simple_data(con)
        con.execute(
            "INSERT INTO orders VALUES(3,10); ALTER TABLE customers ADD COLUMN region_id INT DEFAULT 1"
        )
        con.execute("CREATE TABLE regions(id INT); INSERT INTO regions VALUES(1)")
        c = simple_contract()
        c["primary_keys"]["regions"] = ["id"]
        c["scopes"]["$root"]["joins"]["r"] = {"cardinality": "1:1", "preserve": "left"}
        sql = SIMPLE_SQL + " LEFT JOIN regions r ON c.region_id=r.id"
        bundle = compile_contract(sql, c)
        bad = failures(con, bundle)
        assert {(check.kind, check.join) for check, _ in bad.values()} == {
            ("join_key", "r"),
            ("join_cardinality", "r"),
        }
        assert bundle.joins[1].left_aliases == ("o", "c")


def test_self_join_keeps_aliases_distinct():
    with connection() as con:
        con.execute(
            "CREATE TABLE employees(id INT,manager_id INT); INSERT INTO employees VALUES(1,NULL),(2,1),(3,1)"
        )
        c = {
            "version": 1,
            "primary_keys": {"employees": ["id"]},
            "scopes": {
                "$root": {
                    "grain": ["e.id"],
                    "joins": {"manager": {"cardinality": "m:1", "preserve": "left"}},
                }
            },
            "output_grain": ["id"],
        }
        sql = "SELECT e.id,manager.id manager_id FROM employees e LEFT JOIN employees manager ON e.manager_id=manager.id"
        bundle = compile_contract(sql, c)
        assert sum(check.kind == "primary_key" for check in bundle.checks) == 1
        assert not failures(con, bundle)
        con.execute("INSERT INTO employees VALUES(1,NULL)")
        assert any(
            check.kind == "primary_key"
            for check, _ in failures(con, compile_contract(sql, c)).values()
        )
