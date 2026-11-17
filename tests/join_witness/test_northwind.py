from decimal import Decimal

import duckdb
import pytest
from tableproof import JoinSpec

from datawrench.join_witness import analyze
from datawrench.join_witness.sql import source_sql
from .test_engine import FIXTURES, ROOT

FOLDER = FIXTURES / "northwind"


def test_actual_customer_id_join_preserves_orders(db):
    spec = JoinSpec("customer-id", FOLDER / "orders.csv", FOLDER / "customers.csv",
                    ("CustomerID",), ("CustomerID",), "many-to-one")
    report = analyze(spec, left_measures=("Freight",))
    assert report["verdict"] == "pass"
    assert report["summary"]["predicted_join_rows"] == 830
    assert report["summary"]["left_added_row_copies"] == 0
    assert report["summary"]["contract_violating_matched_key_groups"] == 0
    assert report["top_keys"]  # right customer records repeat across legitimate orders
    assert not any(group["violates_relationship"] for group in report["top_keys"])
    values = report["measures"]["left"]["Freight"]
    assert Decimal(values["predicted_join_sum"]) == Decimal("21129.25")
    assert values["added_by_repetition"] == "0"
    for assertion in report["assertions"]:
        assert db.execute(assertion["sql"]).fetchone() == (True,)


def test_wrong_country_key_explains_the_metric_inflation(db):
    spec = JoinSpec("country", FOLDER / "orders.csv", FOLDER / "customers.csv",
                    ("ShipCountry",), ("Country",), "many-to-one", null_keys="warn")
    report = analyze(spec, left_measures=("Freight",), top=3, sample_rows=2)
    count, freight = db.execute(
        f"SELECT count(*), sum(CAST(l.Freight AS DECIMAL(38,2))) "
        f"FROM {source_sql(spec.left)} l LEFT JOIN {source_sql(spec.right)} r ON l.ShipCountry=r.Country"
    ).fetchone()
    assert report["summary"]["predicted_join_rows"] == count == 5941
    assert report["summary"]["contract_excess_matched_rows"] == 5111
    assert Decimal(report["measures"]["left"]["Freight"]["predicted_join_sum"]) == freight == Decimal("152685.50")
    worst = report["top_keys"][0]
    assert worst["key"] == ["USA"]
    assert (worst["left_count"], worst["right_count"], worst["joined_rows"]) == (122, 13, 1586)
    assert worst["left_records"][0]["values"]["OrderID"] == "10262"
    assert worst["left_records"][0]["join_copies"] == 13
    unique = next(item for item in report["assertions"] if item["name"] == "right_unique")
    with pytest.raises(duckdb.InvalidInputException, match="must be unique"):
        db.execute(unique["sql"])


def test_northwind_metric_order_matches_materialized_join_attribution(db):
    spec = JoinSpec("metric-country", FOLDER / "orders.csv", FOLDER / "customers.csv",
                    ("ShipCountry",), ("Country",), "many-to-one", null_keys="warn")
    report = analyze(spec, rank_by="left:Freight", top=3)
    query = (
        f"WITH original AS (SELECT ShipCountry AS country, sum(CAST(Freight AS DECIMAL(38,2))) AS amount "
        f"FROM {source_sql(spec.left)} GROUP BY ShipCountry), "
        f"joined AS (SELECT l.ShipCountry AS country, sum(CAST(l.Freight AS DECIMAL(38,2))) AS amount "
        f"FROM {source_sql(spec.left)} l LEFT JOIN {source_sql(spec.right)} r ON l.ShipCountry=r.Country "
        f"GROUP BY l.ShipCountry) SELECT j.country, j.amount-o.amount AS added FROM joined j JOIN original o "
        f"ON j.country=o.country ORDER BY abs(added) DESC, j.country LIMIT 3"
    )
    actual = db.execute(query).fetchall()
    assert [(group["key"][0], Decimal(group["ranking_measure_added"])) for group in report["top_keys"]] == actual
