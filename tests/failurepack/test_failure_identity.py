"""Adversarial cases where broad error labels can switch the selected rule."""

import pytest

# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

import pandas as pd
import pandera.pandas as pa

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle


def test_separate_compound_uniqueness_rules_do_not_share_an_identity(tmp_path):
    data, schema = tmp_path / "compound.csv", tmp_path / "schema.yaml"
    pd.DataFrame(
        {
            "a": [0, 1, 10, 10],
            "b": [0, 1, 10, 10],
            "c": [99, 99, 2, 3],
            "d": [99, 99, 2, 3],
        }
    ).to_csv(data, index=False)
    pa.DataFrameSchema(
        {name: pa.Column(int) for name in ["a", "b", "c", "d"]},
        unique=[["a", "b"], ["c", "d"]],
    ).to_yaml(schema)
    failures = list_failures(data, schema, ",", {}, 10, 32 * 1024 * 1024)
    target = next(f for f in failures if f["check"] == "multiple_fields_uniqueness")
    assert target["unique_columns"] == ["a", "b"]
    output = tmp_path / "repro"
    manifest = reduce_to_bundle(data, schema, output, target["id"])
    assert manifest["result"]["source_record_ranges"] == [[3, 4]]
    assert pd.read_csv(output / "data.csv").a.tolist() == [10, 10]


def test_multiindex_level_checks_remain_distinct(tmp_path):
    data, schema = tmp_path / "indexed.csv", tmp_path / "schema.py"
    pd.DataFrame(
        {
            "one": [1, 1, -1, 3],
            "two": [-1, 2, 3, 3],
            "value": [1, 2, 3, 4],
        }
    ).to_csv(data, index=False)
    schema.write_text(
        "import pandera.pandas as pa\n"
        "schema=pa.DataFrameSchema({'value':pa.Column(int)}, index=pa.MultiIndex([\n"
        " pa.Index(int,pa.Check.ge(0),name='one'),\n"
        " pa.Index(int,pa.Check.ge(0),name='two')]))\n"
    )
    options = {"index_col": ["one", "two"]}
    failures = list_failures(data, schema, ",", options, 10, 32 * 1024 * 1024)
    checks = [f for f in failures if f["reason"] == "dataframe_check"]
    assert len(checks) == 2
    assert {f["column"] for f in checks} == {"one", "two"}
    target = next(f["id"] for f in checks if f["column"] == "one")
    manifest = reduce_to_bundle(data, schema, tmp_path / "repro", target, options=options)
    assert manifest["result"]["source_record_ranges"] == [[3, 3]]


def test_compound_uniqueness_keeps_nullable_column_context(tmp_path):
    data, schema = tmp_path / "nullable.csv", tmp_path / "schema.yaml"
    pd.DataFrame(
        {
            "a": [0.0, 1.0, 10.0, 10.0],
            "b": [0.0, 1.0, float("nan"), float("nan")],
            "c": [99.0, 99.0, 2.0, 3.0],
            "d": [99.0, 99.0, 2.0, 3.0],
        }
    ).to_csv(data, index=False)
    pa.DataFrameSchema(
        {name: pa.Column(float, nullable=True) for name in ["a", "b", "c", "d"]},
        unique=[["a", "b"], ["c", "d"]],
    ).to_yaml(schema)
    failures = list_failures(data, schema, ",", {}, 10, 32 * 1024 * 1024)
    selected = next(f for f in failures if f["check"] == "multiple_fields_uniqueness")
    assert selected["unique_columns"] == ["a", "b"]
    manifest = reduce_to_bundle(data, schema, tmp_path / "output", selected["id"])
    assert manifest["result"]["source_record_ranges"] == [[3, 4]]
