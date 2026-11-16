"""Sequence-sensitive typed tuples over independent, sorted real-Parquet scans."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main


def options(names, flag="--ordered-row-checksum-column"):
    return [arg for name in names for arg in (flag, name)]


def validate(source, output, capsys, *args):
    code = main(["validate", str(source), "--output", str(output), *args])
    captured = capsys.readouterr()
    assert not captured.err
    return code, json.loads(captured.out)


def golden(schema, rows):
    # Independent wire oracle: explicit tagged, byte-length-framed JSON/ASCII.
    def compact(value):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    frames = [(b"H", compact(["parquet-unify-ordered-row-v1", schema]))]
    frames += [(b"R", compact(row)) for row in rows]
    frames += [(b"C", str(len(rows)).encode())]
    return hashlib.sha256(b"".join(tag + len(payload).to_bytes(8, "big") + payload
                                   for tag, payload in frames)).hexdigest()


def test_ordered_reverse_rows_regression(tmp_path, capsys):
    source = tmp_path / "input"
    source.mkdir()
    table = pa.table({"id": [1, 2, 3], "note": ["a", "b", "c"]})
    pq.write_table(table.slice(0, 2), source / "a.parquet")
    pq.write_table(table.slice(2), source / "b.parquet")
    output = tmp_path / "reverse.parquet"
    pq.write_table(table.take(pa.array([2, 1, 0])), output)
    code, report = validate(source, output, capsys, *options(["id", "note", "id"]),
                            *options(["id", "note"], "--row-checksum-column"),
                            "--checksum-column", "id", "--null-column", "note")
    assert code == 1 and not report["ok"]
    assert all(c["ok"] for k, c in report["checks"].items() if k != "ordered_row_checksum")
    schema = [{"name": "id", "type": "int64"}, {"name": "note", "type": "string"}]
    expected = golden(schema, [[1, "a"], [2, "b"], [3, "c"]])
    actual = golden(schema, [[3, "c"], [2, "b"], [1, "a"]])
    assert actual != expected
    assert report["expected"]["ordered_row_checksum"] == expected
    assert report["actual"]["ordered_row_checksum"] == actual
    assert report["checks"]["ordered_row_checksum"] == {
        "ok": False, "expected": expected, "actual": actual, "unavailable": [],
        "expected_by_file": [
            {"file": str(source / "a.parquet"), "checksum": golden(schema, [[1, "a"], [2, "b"]])},
            {"file": str(source / "b.parquet"), "checksum": golden(schema, [[3, "c"]])}],
        "actual_by_file": [{"file": str(output), "checksum": actual}]}
    assert report["scope"]["ordered_row_checksum_columns"] == ["id", "note"]
    assert report["scope"]["values_checked"] is True


@pytest.mark.parametrize("batch_size", [1, 2, 65536])
@pytest.mark.parametrize("empty", [False, True])
def test_split_boundaries_duplicates_nulls_and_string_path_order(tmp_path, capsys, batch_size, empty):
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    table = pa.table({"id": pa.array([1, 1, None, 2, 2], pa.int64()),
                      "note": ["é", "é", None, "", ""]})
    if empty:
        table = table.slice(0, 0)
    # Path-component traversal puts a/z before a.parquet; absolute STRING sort
    # puts a.parquet first. Only the latter is the selected logical sequence.
    (source / "a").mkdir()
    pq.write_table(table.slice(2), source / "a" / "z.parquet", row_group_size=1)
    pq.write_table(table.slice(0, 2), source / "a.parquet", row_group_size=2)
    pq.write_table(table.slice(0, 0), source / "z-empty.parquet")
    pq.write_table(table.slice(0, 1), output / "different-first.parquet")
    pq.write_table(table.slice(1), output / "different-last.parquet", row_group_size=3)
    args = [*options(["id", "note"]), "--batch-size", str(batch_size)]
    code, report = validate(source, output, capsys, *args)
    assert code == 0
    rows = [] if empty else [[1, "é"], [1, "é"], [None, None], [2, ""], [2, ""]]
    schema = [{"name": "id", "type": "int64"}, {"name": "note", "type": "string"}]
    assert report["expected"]["ordered_row_checksum"] == golden(schema, rows)
    assert report["checks"]["ordered_row_checksum"]["unavailable"] == []
    for side in ["expected", "actual"]:
        assert sum(r["rows"] for r in report["checks"]["rows"][side + "_by_file"]) == len(rows)
    if not empty:
        # Same count, different duplicate multiplicity; null tuples also matter.
        pq.write_table(table.take(pa.array([0, 0, 0, 3])), output / "different-last.parquet")
        assert validate(source, output, capsys, *args)[0] == 1


def test_literal_selection_order_type_domain_and_omission(tmp_path, capsys, monkeypatch):
    import datawrench.parquet_unify.validation as validation
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    table = pa.table({"a.b": [1, 2], "^x$": ["é", ""], "*": [1, 2]})
    pq.write_table(table, source)
    pq.write_table(table, output)
    digests = []
    for names in [["a.b", "^x$", "a.b"], ["^x$", "a.b"], ["*", "^x$"]]:
        code, report = validate(source, output, capsys, *options(names))
        assert code == 0
        assert report["scope"]["ordered_row_checksum_columns"] == list(dict.fromkeys(names))
        digests.append(report["expected"]["ordered_row_checksum"])
    assert len(set(digests)) == 3
    for t in [pa.int64(), pa.timestamp("ns")]:
        pq.write_table(pa.table({"v": pa.array([1, 2], t)}), source)
        pq.write_table(pa.table({"v": pa.array([1, 2], t)}), output)
        digests.append(validate(source, output, capsys, *options(["v"]))[1]["expected"]["ordered_row_checksum"])
    assert digests[-1] != digests[-2]

    def fail(*args, **kwargs):
        raise AssertionError("omitted option must add no hashing or data decoding")
    monkeypatch.setattr(validation, "OrderedRowSummary", fail)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    code, report = validate(source, output, capsys)
    assert code == 0 and report["scope"]["values_checked"] is False
    assert all("ordered_row_checksum" not in section for section in report.values() if isinstance(section, dict))


@pytest.mark.parametrize("kind", ["missing", "type", "nested", "order", "duplicate", "unexpected"])
def test_unavailable_exact_schema_evidence_and_independent_selection(tmp_path, capsys, kind):
    source, output = tmp_path / "input.parquet", tmp_path / "output"
    output.mkdir()
    fields = [("x", pa.int64()), ("y", pa.int64())]
    table = pa.table({"id": [1], "p/~": pa.array([None], pa.list_(pa.struct(fields)))})
    pq.write_table(table, source)
    pq.write_table(table, output / "middle.parquet")
    if kind == "missing":
        bad = table.drop(["p/~"])
    elif kind == "type":
        bad = table.set_column(1, "p/~", pa.array(["bad"]))
    else:
        changed = {"nested": fields[:1], "order": fields[::-1],
                   "duplicate": [fields[0], fields[0], fields[1]],
                   "unexpected": [*fields, ("[]", pa.int64())]}[kind]
        bad = table.set_column(1, "p/~", pa.array([None], pa.list_(pa.struct(changed))))
    for name in ["a-bad.parquet", "z-bad.parquet"]:
        pq.write_table(bad.slice(0, 0), output / name)
    code, report = validate(source, output, capsys, *options(["p/~", "id", "p/~"]),
                            *options(["p/~", "id"], "--row-checksum-column"),
                            "--checksum-column", "id", "--null-column", "id")
    assert code == 1 and report["checks"]["rows"]["ok"]
    check = report["checks"]["ordered_row_checksum"]
    assert check["actual"] is None and not check["ok"]
    assert check["unavailable"] == report["checks"]["row_checksum"]["unavailable"]
    assert check["unavailable"] == [{"column": "p/~", **r} for r in report["checks"]["schema"]["issues"]]
    assert report["checks"]["checksums"]["ok"] and report["checks"]["null_counts"]["ok"]
    code, report = validate(source, output, capsys, *options(["id"]))
    assert code == 1 and report["checks"]["ordered_row_checksum"]["ok"]
    assert report["checks"]["ordered_row_checksum"]["unavailable"] == []
    # Root schema order alone is irrelevant to name-selected tuple values.
    for path in output.iterdir():
        path.unlink()
    pq.write_table(table.select(["p/~", "id"]), output / "root-order.parquet")
    code, report = validate(source, output, capsys, *options(["id", "p/~"]))
    assert code == 1 and report["checks"]["ordered_row_checksum"]["ok"]


@pytest.mark.parametrize("case", ["unknown", "case", "nested", "input_budget", "output_budget", "batch_zero", "negative_budget"])
def test_errors_before_decode(tmp_path, capsys, monkeypatch, case):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    pq.write_table(pa.table({"id": [1, 2] if case == "input_budget" else [1]}), source)
    pq.write_table(pa.table({"id": [1, 2]}), output)
    args = options([{"unknown": "missing", "case": "ID", "nested": "id.child"}.get(case, "id")])
    args += {"input_budget": ["--max-rows", "1"], "output_budget": ["--max-rows", "1"],
             "batch_zero": ["--batch-size", "0"], "negative_budget": ["--max-rows", "-1"]}.get(case, [])
    def fail(*args, **kwargs):
        raise AssertionError("input errors and row guards precede every data decode")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    assert main(["validate", str(source), "--output", str(output), *args]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    expected = {"input_budget": "input rows", "output_budget": "output rows", "batch_zero": "batch-size must be positive",
                "negative_budget": "max-rows must be nonnegative"}.get(case, "unknown top-level ordered row checksum columns")
    assert expected in captured.err


def test_hive_planned_values_shared_bounded_passes_and_skips(tmp_path, capsys, monkeypatch):
    import datawrench.parquet_unify.cli as cli
    source = tmp_path / "input"
    for part in ["k=001", "k=__HIVE_DEFAULT_PARTITION__"]:
        (source / part).mkdir(parents=True)
    pq.write_table(pa.table({"id": pa.array([1, 2], pa.int32()), "x": [1, None],
                             "p": pa.array([{"d": 2932897}, None], pa.struct([("d", pa.date32())]))}),
                   source / "k=001" / "a.parquet", row_group_size=2)
    pq.write_table(pa.table({"id": [3], "x": ["001"]}), source / "k=__HIVE_DEFAULT_PARTITION__" / "b.parquet")
    output = tmp_path / "output.parquet"
    schema = pa.schema([("id", pa.int64()), ("x", pa.string()), ("p", pa.struct([("d", pa.date32())])), ("k", pa.string())])
    table = pa.Table.from_pylist([{"id": 1, "x": "1", "p": {"d": 2932897}, "k": "001"},
                                 {"id": 2, "x": None, "p": None, "k": "001"},
                                 {"id": 3, "x": "001", "p": None, "k": None}], schema=schema)
    pq.write_table(table, output, row_group_size=1)
    original = pq.ParquetFile.iter_batches
    passes, sizes = [], []
    def batches(reader, *args, **kwargs):
        assert kwargs == {"batch_size": 1, "use_threads": False}
        passes.append(reader)
        for batch in original(reader, *args, **kwargs):
            sizes.append(batch.num_rows)
            yield batch
    def fail(*args, **kwargs):
        raise AssertionError("no engines or full-file/row-group materialization")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    monkeypatch.setattr(pq.ParquetFile, "read", fail)
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", fail)
    monkeypatch.setattr(cli, "verify", fail)
    monkeypatch.setattr(cli, "rewrite", fail)
    args = [*options(schema.names), *options(schema.names, "--row-checksum-column"), "--checksum-column", "id",
            "--null-column", "p", "--hive", "--policy", "stringify-conflicts", "--batch-size", "1", "--max-rows", "3"]
    code, report = validate(source, output, capsys, *args)
    assert code == 0 and all(c["ok"] for c in report["checks"].values())
    assert len(passes) == 3 and max(sizes) == 1
    assert report["expected"]["null_counts"] == {"p": 2}
    # Duplicate scalar integer and temporal encodings must remain typed; selected
    # field order and full-domain date32 reuse the common canonical encoder.
    assert len(report["expected"]["ordered_row_checksum"]) == 64
    bad = source / "bad.txt"
    bad.write_text("unreadable")
    assert main(["validate", str(source), "--output", str(output), *args]) == 2
    assert "unreadable files" in capsys.readouterr().err
    assert main(["validate", str(source), "--output", str(output), *args, "--skip-unreadable"]) == 0
    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert json.loads(captured.out)["expected"] == report["expected"]
    output.write_text("unreadable")
    assert main(["validate", str(source), "--output", str(output), *args, "--skip-unreadable"]) == 2
    assert "output unreadable" in capsys.readouterr().err


def test_canonical_values_and_stream_finalization(tmp_path, capsys):
    from decimal import Decimal
    from datawrench.parquet_unify.data import OrderedRowSummary
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    table = pa.table({"d": pa.array([-2147483648, 2932897, None], pa.date32()),
                      "n": pa.array([1, None, -1], pa.timestamp("ns")),
                      "f": [float("nan"), -0.0, None],
                      "b": [b"\x00", b"", None],
                      "money": pa.array([Decimal("1.20"), None, Decimal("0.00")], pa.decimal128(6, 2)),
                      "items": pa.array([[1, None], [], None], pa.list_(pa.int64()))})
    pq.write_table(table, source, row_group_size=2)
    other = table.set_column(2, "f", pa.array([float("nan"), 0.0, None]))
    other = other.set_column(5, "items", other["items"].cast(pa.large_list(pa.int64())))
    pq.write_table(other, output, row_group_size=1)
    code, report = validate(source, output, capsys, *options(table.column_names), "--batch-size", "1")
    assert code == 0
    # Exact canonical expected wire values, including full-domain date escapes.
    rows = [[{"date32_days": -2147483648}, 1, "NaN", "00", "1.20", [1, None]],
            [{"date32_days": 2932897}, None, "0000000000000000", "", None, []],
            [None, -1, None, None, "0.00", None]]
    assert report["expected"]["ordered_row_checksum"] == golden(report["expected"]["schema"], rows)
    summary = OrderedRowSummary(table.schema)
    first = summary.digest()
    assert summary.digest() == first  # Finalization must not alter the streaming state.
    summary.add(table.slice(0, 1))
    prefix = summary.digest()
    summary.add(table.slice(1))
    assert summary.digest() != prefix != first
    assert summary.digest() == report["expected"]["ordered_row_checksum"]
