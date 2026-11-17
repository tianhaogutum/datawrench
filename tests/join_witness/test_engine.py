import csv
import random
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from tableproof import JoinSpec, TableProofError

from datawrench.join_witness import analyze
from datawrench.join_witness.sql import source_sql

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def example_spec(relationship="many-to-one"):
    return JoinSpec("orders", ROOT / "examples/join_witness/orders.csv", ROOT / "examples/join_witness/customers.csv",
                    ("customer_id",), ("customer_id",), relationship)


def test_real_polars_exports_have_source_witnesses_and_exact_sums():
    report = analyze(example_spec(), left_measures=("amount",))
    assert report["verdict"] == "fail"
    assert report["summary"]["predicted_join_rows"] == 6
    assert report["summary"]["contract_excess_matched_rows"] == 2
    assert report["summary"]["left_added_row_copies"] == 2
    group, = report["top_keys"]
    assert group["key"] == ["C001"]
    assert group["joined_rows"] == 4
    assert [record["record_number"] for record in group["left_records"]] == [1, 2]
    assert group["left_records"][0]["values"]["order_id"] == "O101"
    assert group["left_records"][0]["join_copies"] == 2
    values = report["measures"]["left"]["amount"]
    assert Decimal(values["source_sum"]) == Decimal("35")
    assert Decimal(values["predicted_join_sum"]) == Decimal("55")
    assert Decimal(values["added_by_repetition"]) == Decimal("20")
    assert values["dropped_sum"] == "0"


@pytest.mark.parametrize("how,rows,retained,joined,dropped", [
    ("inner", 5, "30", "50", "5"), ("left", 6, "35", "55", "0"),
    ("right", 6, "30", "50", "5"), ("full", 7, "35", "55", "0"),
])
def test_all_join_types_with_real_duckdb(db, how, rows, retained, joined, dropped):
    spec = example_spec()
    report = analyze(spec, how=how, left_measures=("amount",))
    query = (f"SELECT count(*), SUM(CAST(l.amount AS DECIMAL(38,2))) FROM {source_sql(spec.left)} l "
             f"{how.upper()} JOIN {source_sql(spec.right)} r ON l.customer_id=r.customer_id")
    actual_rows, actual_sum = db.execute(query).fetchone()
    assert report["summary"]["predicted_join_rows"] == actual_rows == rows
    values = report["measures"]["left"]["amount"]
    assert Decimal(values["predicted_join_sum"]) == actual_sum == Decimal(joined)
    assert Decimal(values["retained_once_sum"]) == Decimal(retained)
    assert Decimal(values["dropped_sum"]) == Decimal(dropped)


def test_unchanged_tableproof_upstream_example():
    folder = FIXTURES / "tableproof"
    spec = JoinSpec("upstream", folder / "samples.tsv", folder / "results.tsv",
                    ("sample_id",), ("sample_id",), "one-to-many",
                    result=folder / "merged.tsv", join_type="left")
    report = analyze(spec, right_measures=("value",))
    assert report["verdict"] == "pass"
    assert report["summary"]["predicted_join_rows"] == 4
    assert report["upstream_audit"]["result"]["row_delta"] == 0
    assert Decimal(report["measures"]["right"]["value"]["predicted_join_sum"]) == Decimal("3.4")
    assert Decimal(report["measures"]["right"]["value"]["dropped_sum"]) == Decimal("2.1")


def test_random_real_csv_counts_and_sums_match_duckdb(tmp_path, db):
    rng = random.Random(32108)
    for trial in range(30):
        frames = []
        for side in ("left", "right"):
            size = rng.randrange(0, 14)
            frame = pl.DataFrame({
                "key": [rng.choice(["A", "B", "C", "001", "1", "", None]) for _ in range(size)],
                "amount": [rng.choice(["0.01", "-2.50", "3.00", None]) for _ in range(size)],
            }, schema={"key": pl.String, "amount": pl.String})
            path = tmp_path / f"{side}.csv"
            frame.write_csv(path)
            frames.append(path)
        spec = JoinSpec(str(trial), *frames, ("key",), ("key",), "many-to-many", null_keys="ignore")
        for how in ("inner", "left", "right", "full"):
            report = analyze(spec, how=how, left_measures=("amount",), right_measures=("amount",), sample_rows=1)
            query = (f"SELECT count(*), coalesce(SUM(CAST(l.amount AS DECIMAL(38,2))),0), "
                     f"coalesce(SUM(CAST(r.amount AS DECIMAL(38,2))),0) FROM {source_sql(frames[0])} l "
                     f"{how.upper()} JOIN {source_sql(frames[1])} r ON l.key=r.key")
            count, left_sum, right_sum = db.execute(query).fetchone()
            assert report["summary"]["predicted_join_rows"] == count, (trial, how)
            for side, actual in (("left", left_sum), ("right", right_sum)):
                assert Decimal(report["measures"][side]["amount"]["predicted_join_sum"]) == actual, (trial, how, side)


def test_multiline_bom_composite_keys_have_physical_line_locators(tmp_path):
    paths = [tmp_path / "left.tsv", tmp_path / "right.tsv"]
    for path in paths:
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(["account", "period", "note"])
            writer.writerows([["001", "2026", "hello\nworld"], ["001", "2026", "x"]])
    report = analyze(JoinSpec("composite", *paths, ("account", "period"), ("account", "period"), "one-to-one"))
    record = report["top_keys"][0]["left_records"][0]
    assert (record["record_number"], record["line_start"], record["line_end"]) == (1, 2, 3)
    assert record["values"]["note"] == "hello\nworld"


def test_redaction_and_sample_bounds():
    report = analyze(example_spec(), redact=True, sample_rows=1)
    assert "key" not in report["top_keys"][0]
    assert "values" not in report["top_keys"][0]["left_records"][0]
    assert report["top_keys"][0]["left_records_omitted"] == 1
    assert report["upstream_audit"]["right"]["duplicates"]["examples"][0].startswith("sha256:")
    zero = analyze(example_spec(), top=0)
    assert zero["summary"]["left_added_row_copies"] == 2
    assert zero["summary"]["omitted_key_groups"] == 1
    assert zero["top_keys"] == []


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1_000", " 1", "$10", "1e99999", "1,000"])
def test_invalid_measures_are_errors(tmp_path, value):
    path = tmp_path / "bad.csv"
    pl.DataFrame({"customer_id": ["C001"], "amount": [value]}).write_csv(path)
    spec = JoinSpec("bad", path, example_spec().right, ("customer_id",), ("customer_id",), "many-to-many")
    with pytest.raises(TableProofError, match="numeric|Numeric"):
        analyze(spec, left_measures=("amount",))
