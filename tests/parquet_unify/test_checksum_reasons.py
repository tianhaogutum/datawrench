"""Per-column checksum unavailability reuses exact selected schema evidence."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main
from datawrench.parquet_unify.data import Summary


def validate(source, output, capsys, *options):
    code = main(["validate", str(source), "--output", str(output), *options])
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
def test_checksum_unavailable_empty_bad_file(tmp_path, capsys, kind, paths):
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
    options = ["--checksum-column", "p/~", "--checksum-column", "id", "--checksum-column", "p/~"]
    code, report = validate(source, output, capsys, *options)
    assert code == 1 and report["actual"]["checksums"]["p/~"] is None
    assert report["checks"]["rows"]["ok"]
    checks = report["checks"]["checksums"]["columns"]
    assert list(checks) == ["p/~", "id"]
    assert checks["p/~"]["expected_by_file"] == [{"file": str(source), "checksum": checks["p/~"]["expected"]}]
    assert checks["p/~"]["actual_by_file"] == [
        {"file": str(output / "a.parquet"), "checksum": checks["p/~"]["expected"]},
        {"file": str(output / "z.parquet"), "checksum": None}]
    empty_id = Summary(pa.schema([table.schema.field("id")])).report()["checksums"]["id"]
    assert checks["id"]["actual_by_file"] == [
        {"file": str(output / "a.parquet"), "checksum": checks["id"]["expected"]},
        {"file": str(output / "z.parquet"), "checksum": empty_id}]
    assert checks["id"]["ok"] and checks["id"]["unavailable"] == []
    reasons = checks["p/~"]["unavailable"]
    assert [(r["path_parts"], r["reason"]) for r in reasons] == paths
    assert {json.dumps(r, sort_keys=True) for r in reasons} == {
        json.dumps({"column": "p/~", **issue}, sort_keys=True) for issue in report["checks"]["schema"]["issues"]}
    assert len(reasons) == len({json.dumps(r, sort_keys=True) for r in reasons})
    assert all(r["file"] == str(output / "z.parquet") and r["path"].startswith("/p~1~0") for r in reasons)
    # Independent selections: a row tuple containing only id remains available.
    code, combined = validate(source, output, capsys, *options, "--row-checksum-column", "id")
    assert code == 1 and combined["checks"]["row_checksum"]["ok"]
    assert combined["checks"]["row_checksum"]["unavailable"] == []
    assert combined["checks"]["checksums"] == report["checks"]["checksums"]
    code, healthy = validate(source, output, capsys, "--checksum-column", "id")
    assert code == 1 and healthy["checks"]["checksums"]["ok"]
    assert healthy["checks"]["checksums"]["columns"]["id"] == checks["id"]
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before}


def test_checksum_reasons_stable_order_dedup_and_shared_pass(tmp_path, capsys, monkeypatch):
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
    code, report = validate(source, output, capsys, "--checksum-column", "b", "--checksum-column", "a",
                            "--checksum-column", "b", "--checksum-column", "id", "--null-column", "id",
                            "--row-checksum-column", "b", "--row-checksum-column", "a", "--batch-size", "1")
    assert code == 1 and report["scope"]["checksum_columns"] == ["b", "a", "id"]
    checks = report["checks"]["checksums"]["columns"]
    combined = [r for c in checks.values() for r in c["unavailable"]]
    assert [(r["column"], r["file"]) for r in combined] == [
        (col, str(output / file)) for col in ["b", "a"] for file in ["m.parquet", "z.parquet"]]
    assert report["checks"]["row_checksum"]["unavailable"] == combined
    assert schema_calls == [str(output / f) for f in ["a.parquet", "m.parquet", "z.parquet"]]
    assert len(passes) == 4  # Null/column/tuple metrics share a single pass per file.


@pytest.mark.parametrize("kind", ["correct", "reorder", "changed", "root_order", "unselected"])
def test_available_checksum_has_no_unavailability_reason(tmp_path, capsys, kind):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    table = pa.table({"id": [1, 2], "note": ["a", "b"], "other": [3, 4]})
    pq.write_table(table, source)
    other = table
    if kind == "reorder":
        other = table.take(pa.array([1, 0]))
    elif kind == "changed":
        other = table.set_column(1, "note", pa.array(["b", "b"]))
    elif kind == "root_order":
        other = table.select(["note", "other", "id"])
    elif kind == "unselected":
        other = table.drop(["other"])
    pq.write_table(other, output)
    code, report = validate(source, output, capsys, "--checksum-column", "note")
    assert code == (0 if kind in ["correct", "reorder"] else 1)
    check = report["checks"]["checksums"]["columns"]["note"]
    assert check["unavailable"] == [] and check["actual"] is not None
    assert check["ok"] == (kind != "changed")
