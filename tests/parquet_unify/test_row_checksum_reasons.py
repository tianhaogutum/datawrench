"""Selected tuple unavailability retains exact schema evidence from real Parquet."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main


def validate(source, output, capsys, names, *extra):
    args = [arg for name in names for arg in ("--row-checksum-column", name)]
    code = main(["validate", str(source), "--output", str(output), *args, *extra])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("kind,paths", [
    ("missing", [(["p/~"], "missing_field")]),
    ("type", [(["p/~"], "type")]),
    ("nested", [(["p/~", None, "x"], "missing_field"),
                (["p/~", None, "z"], "unexpected_field")]),
    ("order", [(["p/~", None], "field_order")]),
    ("duplicate", [(["p/~", None], "duplicate_fields"), (["p/~", None], "field_order")]),
    ("duplicate_extra", [(["p/~", None], "duplicate_fields"),
                         (["p/~", None, "z"], "unexpected_field")]),
    ("literal_brackets", [(["p/~", None, "[]"], "type")]),
])
def test_row_checksum_unavailable_empty_bad_file(tmp_path, capsys, kind, paths):
    source, output = tmp_path / "input.parquet", tmp_path / "output"
    output.mkdir()
    fields = [("x", pa.int64()), ("y", pa.int64()), ("[]", pa.int64())]
    table = pa.table({"id": [1], "p/~": pa.array([None], pa.list_(pa.struct(fields)))})
    pq.write_table(table, source)
    pq.write_table(table, output / "a.parquet")
    if kind == "missing":
        bad = table.drop(["p/~"])
    elif kind == "type":
        bad = table.set_column(1, "p/~", pa.array(["wrong"]))
    else:
        changed = {"nested": [fields[1], fields[2], ("z", pa.int64())],
                   "order": list(reversed(fields)),
                   "duplicate": [fields[0], fields[0], *fields[1:]],
                   "duplicate_extra": [*fields, ("z", pa.int64()), ("z", pa.int64())],
                   "literal_brackets": [*fields[:2], ("[]", pa.string())]}[kind]
        bad = table.set_column(1, "p/~", pa.array([None], pa.list_(pa.struct(changed))))
    pq.write_table(bad.slice(0, 0), output / "z.parquet")
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
              for p in [source, *output.iterdir()]}
    code, report = validate(source, output, capsys, ["p/~", "id", "p/~"], "--checksum-column", "id")
    assert code == 1 and report["actual"]["row_checksum"] is None
    assert report["checks"]["rows"]["ok"] and report["checks"]["checksums"]["ok"]
    reasons = report["checks"]["row_checksum"]["unavailable"]
    assert [(r["path_parts"], r["reason"]) for r in reasons] == paths
    evidence = [{"column": "p/~", **issue} for issue in report["checks"]["schema"]["issues"]]
    assert all(r in evidence for r in reasons)
    assert {json.dumps(r, sort_keys=True) for r in reasons} == {json.dumps(r, sort_keys=True) for r in evidence}
    assert len(reasons) == len({json.dumps(r, sort_keys=True) for r in reasons})
    assert all(r["file"] == str(output / "z.parquet") and r["path"].startswith("/p~1~0") for r in reasons)
    # Same bad file remains a schema failure when only the healthy id is selected.
    code, report = validate(source, output, capsys, ["id"])
    assert code == 1 and report["checks"]["row_checksum"]["ok"]
    assert report["checks"]["row_checksum"]["unavailable"] == []
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before}


def test_row_checksum_unavailable_order_dedup_and_shared_evidence(tmp_path, capsys, monkeypatch):
    import datawrench.parquet_unify.validation as validation
    source, output = tmp_path / "input.parquet", tmp_path / "output"
    output.mkdir()
    table = pa.table({"id": [1], "a": ["a"], "b": ["b"]})
    pq.write_table(table, source)
    pq.write_table(table, output / "a.parquet")
    for name in ["z.parquet", "m.parquet"]:
        pq.write_table(table.drop(["a", "b"]).slice(0, 0), output / name)
    original_issues, original_batches = validation._schema_issues, pq.ParquetFile.iter_batches
    schema_calls, passes = [], []

    def issues(actual, target, file):
        schema_calls.append(str(file))
        return original_issues(actual, target, file)

    def batches(reader, *args, **kwargs):
        assert kwargs["use_threads"] is False
        passes.append(reader)
        return original_batches(reader, *args, **kwargs)

    monkeypatch.setattr(validation, "_schema_issues", issues)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    code, report = validate(source, output, capsys, ["b", "a", "b"],
                            "--checksum-column", "id", "--null-column", "id", "--batch-size", "1")
    assert code == 1 and report["scope"]["row_checksum_columns"] == ["b", "a"]
    reasons = report["checks"]["row_checksum"]["unavailable"]
    assert [(r["column"], r["file"]) for r in reasons] == [
        (col, str(output / file)) for col in ["b", "a"] for file in ["m.parquet", "z.parquet"]]
    assert all({k: v for k, v in r.items() if k != "column"} in report["checks"]["schema"]["issues"] for r in reasons)
    assert schema_calls == [str(output / f) for f in ["a.parquet", "m.parquet", "z.parquet"]]
    assert len(passes) == 4  # Same single pass per file for row/column/null metrics.


@pytest.mark.parametrize("kind", ["correct", "reorder", "swapped", "root_order", "unselected"])
def test_row_checksum_available_value_and_schema_failures(tmp_path, capsys, kind):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    table = pa.table({"id": [1, 2], "note": ["a", "b"], "other": [3, 4]})
    pq.write_table(table, source)
    other = table
    if kind == "reorder":
        other = table.take(pa.array([1, 0]))
    elif kind == "swapped":
        other = table.set_column(1, "note", pa.array(["b", "a"]))
    elif kind == "root_order":
        other = table.select(["note", "other", "id"])
    elif kind == "unselected":
        other = table.drop(["other"])
    pq.write_table(other, output)
    code, report = validate(source, output, capsys, ["id", "note"])
    assert code == (0 if kind in ["correct", "reorder"] else 1)
    assert report["checks"]["row_checksum"]["unavailable"] == []
    assert report["actual"]["row_checksum"] is not None
    assert report["checks"]["row_checksum"]["ok"] == (kind != "swapped")
