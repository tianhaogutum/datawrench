"""Null-count availability follows physical parent presence, not child schema validity."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main


def validate(source, output, capsys, *options):
    code = main(["validate", str(source), "--output", str(output), *options])
    captured = capsys.readouterr()
    assert not captured.err
    return code, json.loads(captured.out)


@pytest.mark.parametrize("name", ["profile", "p/~", "a.b", "[]", "^x$", "*"])
def test_null_count_missing_empty_files_exact_evidence_and_order(tmp_path, capsys, name):
    source, output = tmp_path / "input.parquet", tmp_path / "output"
    output.mkdir()
    table = pa.table({"id": [1], name: pa.array([None], pa.struct([("x", pa.int64())])),
                      "other": ["kept"]})
    pq.write_table(table, source)
    pq.write_table(table, output / "m.parquet")
    # A missing field before and after the valid file must never regain a count.
    for file in ["z.parquet", "a.parquet"]:
        pq.write_table(table.drop([name, "other"]).slice(0, 0), output / file)
    files = [source, *output.iterdir()]
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    code, report = validate(source, output, capsys, "--null-column", name, "--null-column", "id",
                            "--null-column", name, "--checksum-column", "id", "--row-checksum-column", "id")
    assert code == 1 and report["checks"]["rows"]["ok"]
    assert report["scope"]["null_columns"] == [name, "id"]
    assert report["actual"]["null_counts"] == {name: None, "id": 0}
    checks = report["checks"]["null_counts"]["columns"]
    assert list(checks) == [name, "id"]
    assert checks["id"] == {
        "ok": True, "expected": 0, "actual": 0, "unavailable": [],
        "expected_by_file": [{"file": str(source), "nulls": 0}],
        "actual_by_file": [{"file": str(output / file), "nulls": 0}
                           for file in ["a.parquet", "m.parquet", "z.parquet"]]}
    assert checks[name]["actual_by_file"] == [
        {"file": str(output / "a.parquet"), "nulls": None},
        {"file": str(output / "m.parquet"), "nulls": 1},
        {"file": str(output / "z.parquet"), "nulls": None}]
    reasons = checks[name]["unavailable"]
    assert not checks[name]["ok"] and checks[name]["expected"] == 1
    assert [(r["column"], r["file"], r["path_parts"], r["reason"]) for r in reasons] == [
        (name, str(output / file), [name], "missing_field") for file in ["a.parquet", "z.parquet"]]
    assert reasons == [{"column": name, **i} for i in report["checks"]["schema"]["issues"]
                       if i["path_parts"] == [name]]
    assert len(reasons) == len({json.dumps(r, sort_keys=True) for r in reasons})
    assert report["checks"]["checksums"]["ok"] and report["checks"]["row_checksum"]["ok"]
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}


@pytest.mark.parametrize("kind", ["correct", "nested_missing", "nested_type", "nested_order", "nested_duplicate",
                                 "list_child", "top_type", "root_order", "unselected", "changed_count"])
def test_parent_counts_remain_available_despite_schema_defects(tmp_path, capsys, kind):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    fields = [("x", pa.int64()), ("y", pa.int64())]
    dtype = pa.list_(pa.struct(fields)) if kind == "list_child" else pa.struct(fields)
    values = [None, []] if kind == "list_child" else [None, {}]
    table = pa.table({"id": [1, 2], "p/~": pa.array(values, dtype), "other": [3, 4]})
    pq.write_table(table, source)
    bad = table
    changed = {"nested_missing": [fields[1]], "nested_type": [("x", pa.string()), fields[1]],
               "nested_order": list(reversed(fields)), "nested_duplicate": [fields[0], fields[0], fields[1]]}
    if kind in changed:
        bad = table.set_column(1, "p/~", pa.array(values, pa.struct(changed[kind])))
    elif kind == "list_child":
        bad = table.set_column(1, "p/~", pa.array(values, pa.list_(pa.struct([fields[1]]))))
    elif kind == "top_type":
        bad = table.set_column(1, "p/~", pa.array([None, "value"]))
    elif kind == "root_order":
        bad = table.select(["p/~", "id", "other"])
    elif kind == "unselected":
        bad = table.drop(["other"])
    elif kind == "changed_count":
        bad = table.set_column(1, "p/~", pa.array([{}, {}], dtype))
    pq.write_table(bad, output)
    code, report = validate(source, output, capsys, "--null-column", "p/~", "--checksum-column", "id",
                            "--row-checksum-column", "id", "--batch-size", "1")
    assert code == (0 if kind == "correct" else 1)
    check = report["checks"]["null_counts"]["columns"]["p/~"]
    assert check == {"ok": kind != "changed_count", "expected": 1,
                     "actual": 0 if kind == "changed_count" else 1, "unavailable": [],
                     "expected_by_file": [{"file": str(source), "nulls": 1}],
                     "actual_by_file": [{"file": str(output), "nulls": 0 if kind == "changed_count" else 1}]}
    assert report["checks"]["schema"]["ok"] == (kind in ["correct", "changed_count"])
    assert report["checks"]["checksums"]["ok"] and report["checks"]["row_checksum"]["ok"]


def test_null_reasons_share_schema_evidence_and_metric_passes(tmp_path, capsys, monkeypatch):
    import datawrench.parquet_unify.validation as validation
    source, output = tmp_path / "input.parquet", tmp_path / "output"
    output.mkdir()
    table = pa.table({"id": [1], "a": ["a"], "b": ["b"]})
    pq.write_table(table, source)
    pq.write_table(table, output / "m.parquet")
    for file in ["z.parquet", "a.parquet"]:
        pq.write_table(table.drop(["a", "b"]).slice(0, 0), output / file)
    original_issues, original_batches = validation._schema_issues, pq.ParquetFile.iter_batches
    schema_calls, passes = [], []

    def issues(actual, target, file):
        schema_calls.append(str(file))
        return original_issues(actual, target, file)

    def batches(reader, *args, **kwargs):
        assert kwargs["use_threads"] is False and kwargs["batch_size"] == 1
        passes.append(reader)
        return original_batches(reader, *args, **kwargs)

    monkeypatch.setattr(validation, "_schema_issues", issues)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    code, report = validate(source, output, capsys, "--null-column", "b", "--null-column", "a",
                            "--null-column", "b", "--null-column", "id", "--checksum-column", "b",
                            "--checksum-column", "a", "--checksum-column", "id",
                            "--row-checksum-column", "a", "--row-checksum-column", "id", "--batch-size", "1")
    assert code == 1 and report["scope"]["null_columns"] == ["b", "a", "id"]
    checks = report["checks"]["null_counts"]["columns"]
    reasons = [r for c in checks.values() for r in c["unavailable"]]
    assert [(r["column"], r["file"]) for r in reasons] == [
        (col, str(output / file)) for col in ["b", "a"] for file in ["a.parquet", "z.parquet"]]
    assert reasons == [r for c in report["checks"]["checksums"]["columns"].values() for r in c["unavailable"]]
    assert report["checks"]["row_checksum"]["unavailable"] == checks["a"]["unavailable"]
    assert schema_calls == [str(output / f) for f in ["a.parquet", "m.parquet", "z.parquet"]]
    assert len(passes) == 4  # Source + three artifacts, with null/column/tuple metrics sharing each pass.
