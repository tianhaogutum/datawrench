"""Real Parquet footers exercise complete, literal schema provenance."""
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.catalog import plan, scan
from datawrench.parquet_unify.cli import main
from datawrench.parquet_unify.types import Conflict


def write_schema(path, fields):
    schema = pa.schema(fields)
    pq.write_table(pa.Table.from_batches([], schema=schema), path)


@pytest.fixture
def provenance_input(tmp_path):
    # Empty real files suffice: planning must explain schema bounds, not sample values.
    write_schema(tmp_path / "b.parquet", [
        ("profile/~^*$", pa.struct([("amount/x", pa.decimal128(9, 5)),
                                   ("city~*", pa.string())])),
        ("risk/^n$", pa.float64()),
        ("second*", pa.decimal128(12, 2)),
    ])
    write_schema(tmp_path / "a.parquet", [
        ("profile/~^*$", pa.struct([("amount/x", pa.decimal128(9, 1))])),
        ("risk/^n$", pa.int64()),
        ("second*", pa.float64()),
    ])
    return tmp_path


def test_explain_collects_all_conflicts_and_bounds(provenance_input, capsys):
    root = provenance_input
    args = ["plan", str(root), "--explain", "--format", "json"]
    assert main(args) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert not report["ok"] and report["schema"] is None
    assert report["policy"] == "widen" and "no lossless" in report["error"]
    assert "conflict:" in captured.err
    columns = report["columns"]
    assert [c["path"] for c in columns] == sorted(c["path"] for c in columns)
    fields = {tuple(c["path_parts"]): c for c in columns}
    decimal = fields[("profile/~^*$", "amount/x")]
    assert decimal["path"] == "/profile~1~0^*$/amount~1x"
    assert decimal["unified"] == "decimal128(13, 5)" and decimal["reason"] is None
    assert decimal["files"] == {str(root / "a.parquet"): "decimal128(9, 1)",
                                str(root / "b.parquet"): "decimal128(9, 5)"}
    missing = fields[("profile/~^*$", "city~*")]
    assert missing["path"] == "/profile~1~0^*$/city~0*"
    assert missing["missing_in_some"] and missing["unified"] == "string"
    assert missing["files"] == {str(root / "a.parquet"): None, str(root / "b.parquet"): "string"}
    for key, types in [(('risk/^n$',), ["int64", "double"]),
                       (('second*',), ["double", "decimal128(12, 2)"])]:
        conflict = fields[key]
        assert conflict["classification"] == "conflict" and conflict["unified"] is None
        assert "no lossless common type" in conflict["reason"]
        assert list(conflict["files"].values()) == types
    # Reports do not change when regenerated; neither conflict becomes a string.
    assert main(args) == 1
    assert capsys.readouterr().out == captured.out
    assert main(args + ["--policy", "stringify-conflicts"]) == 1
    assert json.loads(capsys.readouterr().out)["schema"] is None


@pytest.mark.parametrize("fmt", ["text", "json", "md"])
def test_success_explanation_and_explicit_stringification(tmp_path, capsys, fmt):
    write_schema(tmp_path / "a.parquet", [("value", pa.int32())])
    write_schema(tmp_path / "b.parquet", [("value", pa.string()), ("added", pa.int16())])
    assert main(["plan", str(tmp_path), "--explain", "--format", fmt,
                 "--policy", "stringify-conflicts"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    output = captured.out
    if fmt == "md":
        assert output.startswith("```json\n")
        output = output.removeprefix("```json\n").removesuffix("```\n")
    report = json.loads(output)
    assert report["ok"] and report["error"] is None
    assert report["schema"] == [{"name": "value", "type": "string"}, {"name": "added", "type": "int16"}]
    columns = {c["path"]: c for c in report["columns"]}
    # Lossless bounds remain explicit even when the user opted into stringify.
    assert columns["/value"]["unified"] is None
    assert "no lossless" in columns["/value"]["reason"]
    assert columns["/added"]["files"][str(tmp_path / "a.parquet")] is None


def test_strict_explanation_retains_missing_field_provenance(tmp_path, capsys):
    write_schema(tmp_path / "a.parquet", [("id", pa.int32())])
    write_schema(tmp_path / "b.parquet", [("id", pa.int64()), ("extra", pa.string())])
    assert main(["plan", str(tmp_path), "--explain", "--policy", "strict", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] is None and "strict" in report["error"]
    columns = {c["path"]: c for c in report["columns"]}
    assert columns["/id"]["unified"] == "int64"
    assert columns["/extra"]["missing_in_some"]


def test_literal_list_marker_has_distinct_provenance(tmp_path, capsys):
    write_schema(tmp_path / "a.parquet", [("payload", pa.list_(pa.int64()))])
    write_schema(tmp_path / "b.parquet", [("payload", pa.struct([("[]", pa.string())]))])
    catalog = scan(tmp_path)
    children = [c for c in catalog.report()["columns"] if c["path"] == "/payload/[]"]
    assert len(children) == 2
    fields = {tuple(c["path_parts"]): c for c in children}
    assert fields[("payload", None)]["files"] == {
        str(tmp_path / "a.parquet"): "int64", str(tmp_path / "b.parquet"): None}
    assert fields[("payload", "[]")]["files"] == {
        str(tmp_path / "a.parquet"): None, str(tmp_path / "b.parquet"): "string"}
    assert all(c["classification"] == "missing_in_some" for c in children)
    with pytest.raises(Conflict):
        plan(catalog)
    assert main(["scan", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert 'parts=["payload", null]' in text and 'parts=["payload", "[]"]' in text
    assert main(["plan", str(tmp_path), "--explain", "--format", "json"]) == 1
    first = capsys.readouterr().out
    assert len(json.loads(first)["columns"]) == 3
    assert main(["plan", str(tmp_path), "--explain", "--format", "json"]) == 1
    assert capsys.readouterr().out == first


def order_record(root, file, parts, source, target):
    return {"file": str(root / file), "path": "".join(
                "/[]" if p is None else "/" + p.replace("~", "~0").replace("/", "~1")
                for p in parts),
            "path_parts": parts, "reason": "field_order",
            "source_order": source, "target_order": target}


@pytest.mark.parametrize("fmt", ["text", "json", "md"])
def test_strict_order_only_explanation(tmp_path, capsys, monkeypatch, fmt):
    # Identical leaf types must still explain the strict rejection. Files are
    # deliberately created out of order; a.parquet defines the target order.
    fields = [("id", pa.int64()), ("note", pa.string())]
    write_schema(tmp_path / "b.parquet", fields[::-1])
    write_schema(tmp_path / "a.parquet", fields)
    write_schema(tmp_path / "c.parquet", fields)

    def no_data(*args, **kwargs):
        pytest.fail("order explanation must read footers only")

    monkeypatch.setattr(pq, "read_table", no_data)
    for method in ("read", "read_row_group", "read_row_groups", "iter_batches"):
        monkeypatch.setattr(pq.ParquetFile, method, no_data)
    args = ["plan", str(tmp_path), "--explain", "--policy", "strict", "--format", fmt]
    assert main(args) == 1
    captured = capsys.readouterr()
    output = captured.out
    if fmt == "md":
        output = output.removeprefix("```json\n").removesuffix("```\n")
    report = json.loads(output)
    assert not report["ok"] and report["schema"] is None
    assert "strict" in report["error"] and "conflict:" in captured.err
    assert all(c["classification"] == "identical" for c in report["columns"])
    assert report["order_mismatches"] == [
        order_record(tmp_path, "b.parquet", [], ["note", "id"], ["id", "note"])]
    assert main(args) == 1
    assert capsys.readouterr().out == captured.out

    # Widening may execute that target order; the same evidence remains useful.
    for policy in ("widen", "stringify-conflicts"):
        assert main(["plan", str(tmp_path), "--explain", "--policy", policy, "--format", "json"]) == 0
        accepted = json.loads(capsys.readouterr().out)
        assert accepted["order_mismatches"] == report["order_mismatches"]
        assert [f["name"] for f in accepted["schema"]] == ["id", "note"]
    assert main(["plan", str(tmp_path), "--policy", "strict", "--format", "json"]) == 1
    assert not capsys.readouterr().out  # Existing non-explain failure contract.


def test_nested_struct_order_explanations(tmp_path, capsys):
    forward = [("a/b", pa.int32()), ("~tail", pa.string())]
    for file, fields in (("c.parquet", forward[::-1]), ("b.parquet", forward[::-1]),
                         ("a.parquet", forward)):
        write_schema(tmp_path / file, [
            ("profile/~*", pa.struct(fields)),
            ("items", pa.list_(pa.list_(pa.struct(fields)))),
            ("", pa.struct(fields)),
        ])
    assert main(["plan", str(tmp_path), "--explain", "--policy", "strict", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] is None
    assert report["order_mismatches"] == [
        order_record(tmp_path, file, parts, ["~tail", "a/b"], ["a/b", "~tail"])
        for parts in ([""], ["items", None, None], ["profile/~*"])
        for file in ("b.parquet", "c.parquet")]
    leaves = [c for c in report["columns"] if c["path_parts"][-1] in ("a/b", "~tail")]
    assert len(leaves) == 6 and all(c["classification"] == "identical" for c in leaves)


@pytest.mark.parametrize("second,exit_code", [
    ([("a", pa.int32()), ("b", pa.string())], 0),
    ([("a", pa.int64()), ("b", pa.string())], 1),  # Type drift only.
    ([("b", pa.string())], 1),  # Missing field only.
    ([("a", pa.int32()), ("b", pa.string()), ("c", pa.bool_())], 1),
])
def test_strict_non_order_drift_has_no_order_records(tmp_path, capsys, second, exit_code):
    write_schema(tmp_path / "a.parquet", [("a", pa.int32()), ("b", pa.string())])
    write_schema(tmp_path / "b.parquet", second)
    assert main(["plan", str(tmp_path), "--explain", "--policy", "strict", "--format", "json"]) == exit_code
    assert json.loads(capsys.readouterr().out)["order_mismatches"] == []


def test_order_drift_with_missing_fields_and_type_conflict(tmp_path, capsys):
    write_schema(tmp_path / "a.parquet", [
        ("profile", pa.struct([("a", pa.int32()), ("b", pa.string()), ("c", pa.int64())]))])
    write_schema(tmp_path / "b.parquet", [
        ("profile", pa.struct([("c", pa.float64()), ("a", pa.int32()), ("d", pa.bool_())]))])
    # Null/missing parents have no child field order, rather than an empty order.
    write_schema(tmp_path / "c.parquet", [("profile", pa.null())])
    write_schema(tmp_path / "d.parquet", [("other", pa.int32())])
    for policy in ("strict", "widen"):
        assert main(["plan", str(tmp_path), "--explain", "--policy", policy, "--format", "json"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["schema"] is None
        assert report["order_mismatches"] == [order_record(
            tmp_path, "b.parquet", ["profile"], ["c", "a", "d"], ["a", "b", "c", "d"])]
        columns = {tuple(c["path_parts"]): c for c in report["columns"]}
        assert columns[("profile", "c")]["classification"] == "conflict"
        assert columns[("profile", "b")]["missing_in_some"]


def test_order_addresses_distinguish_lists_from_literal_markers(tmp_path, capsys):
    forward = [("a", pa.int32()), ("b", pa.string())]
    for file, child in (("a.parquet", pa.list_(pa.struct(forward))),
                        ("b.parquet", pa.list_(pa.struct(forward[::-1]))),
                        ("c.parquet", pa.struct([("[]", pa.struct(forward))])),
                        ("d.parquet", pa.struct([("[]", pa.struct(forward[::-1]))]))):
        write_schema(tmp_path / file, [("payload", child)])
    assert main(["plan", str(tmp_path), "--explain", "--policy", "strict", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] is None
    assert report["order_mismatches"] == [
        order_record(tmp_path, file, ["payload", marker], ["b", "a"], ["a", "b"])
        for file, marker in (("d.parquet", "[]"), ("b.parquet", None))]
