"""Ordered, typed row-tuple multisets over real Parquet, independently of columns."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main


def validate(source, output, capsys, *args):
    code = main(["validate", str(source), "--output", str(output), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def options(names, flag="--row-checksum-column"):
    return [arg for name in names for arg in (flag, name)]


def golden(schema, rows):
    # Independent, explicit wire-format oracle; no production Summary/serializer.
    hashes = [int.from_bytes(hashlib.sha256(json.dumps(
        ["parquet-unify-row-v1", schema, row], ensure_ascii=True, separators=(",", ":")
    ).encode()).digest(), "big") for row in rows]
    a, b = sum(hashes) % (1 << 256), sum(h*h for h in hashes) % (1 << 256)
    return hashlib.sha256(f"{len(rows)}:{a:064x}:{b:064x}".encode()).hexdigest()


def test_row_checksum_detects_reassociation_preserving_columns(tmp_path, capsys):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    table = pa.table({"id": [1, 2], "note": ["a", "b"]})
    pq.write_table(table, source)
    pq.write_table(table.set_column(1, "note", pa.array(["b", "a"])), output)
    # Existing default and selected-column reports remain unchanged.
    code, default = validate(source, output, capsys)
    assert code == 0 and not default["scope"]["values_checked"]
    code, columns = validate(source, output, capsys, *options(table.column_names, "--checksum-column"))
    assert code == 0 and columns["checks"]["checksums"]["ok"]
    assert "row_checksum" not in columns["checks"] and "row_checksum" not in columns["expected"]
    code, report = validate(source, output, capsys, *options(["id", "note", "id"]),
                            *options(table.column_names, "--checksum-column"), "--batch-size", "1")
    assert code == 1 and report["checks"]["checksums"] == columns["checks"]["checksums"]
    assert report["scope"]["row_checksum_columns"] == ["id", "note"]
    assert report["scope"]["values_checked"] is True
    expected = golden([{"name": "id", "type": "int64"}, {"name": "note", "type": "string"}],
                      [[1, "a"], [2, "b"]])
    actual = golden([{"name": "id", "type": "int64"}, {"name": "note", "type": "string"}],
                    [[1, "b"], [2, "a"]])
    assert report["expected"]["row_checksum"] == expected
    assert report["actual"]["row_checksum"] == actual != expected
    assert report["checks"]["row_checksum"] == {
        "ok": False, "expected": expected, "actual": actual, "unavailable": [],
        "expected_by_file": [{"file": str(source), "checksum": expected}],
        "actual_by_file": [{"file": str(output), "checksum": actual}]}
    # Selecting only id deliberately leaves note values unchecked.
    assert validate(source, output, capsys, *options(["id"]))[0] == 0


def test_row_checksum_reorder_repartition_and_duplicate_sensitivity(tmp_path, capsys):
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    table = pa.table({"id": [1, 1, 2], "note": ["a", "a", "b"]})
    pq.write_table(table.slice(0, 2), source / "a.parquet", row_group_size=1)
    pq.write_table(table.slice(2), source / "b.parquet")
    pq.write_table(table.take(pa.array([2, 0, 1])), output / "a.parquet", row_group_size=2)
    pq.write_table(table.slice(0, 0), output / "empty.parquet")
    args = options(table.column_names)
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
              for d in (source, output) for p in d.iterdir()}
    for size in [1, 2, 65536]:
        code, report = validate(source, output, capsys, *args, "--batch-size", str(size))
        assert code == 0 and report["expected"]["row_checksum"] == report["actual"]["row_checksum"]
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before}
    # Equal row count and same distinct tuple set, different duplicate counts.
    pq.write_table(table.take(pa.array([2, 0, 2])), output / "a.parquet")
    code, report = validate(source, output, capsys, *args)
    assert code == 1 and report["checks"]["rows"]["ok"] and not report["checks"]["row_checksum"]["ok"]


def test_row_checksum_literal_selection_order_and_typed_domain(tmp_path, capsys):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    table = pa.table({"a.b": [1, 2], "a": pa.array([{"b": 3}, None], pa.struct([("b", pa.int64())])),
                      "*": ["a", "b"], "^x$": [1, 2], "x/y~": ["a", "b"]})
    pq.write_table(table, source)
    pq.write_table(table, output)
    code, report = validate(source, output, capsys, *options(["*", "a.b", "*", "a", "^x$", "x/y~"]))
    assert code == 0 and report["scope"]["row_checksum_columns"] == ["*", "a.b", "a", "^x$", "x/y~"]
    digests = []
    for names in [["a.b", "*"], ["*", "a.b"], ["^x$", "x/y~"]]:
        code, report = validate(source, output, capsys, *options(names))
        assert code == 0
        digests.append(report["expected"]["row_checksum"])
    assert len(set(digests)) == 3  # Selection order and literal field identity affect encoding.
    # Int and timestamp both canonicalize to JSON integers, but types distinguish tuples.
    for t in [pa.int64(), pa.timestamp("ns")]:
        pq.write_table(pa.table({"v": pa.array([1, 2], t)}), source)
        pq.write_table(pa.table({"v": pa.array([1, 2], t)}), output)
        digests.append(validate(source, output, capsys, *options(["v"]))[1]["expected"]["row_checksum"])
    assert digests[-1] != digests[-2]


@pytest.mark.parametrize("name", ["ID", "missing", "id.child"])
def test_row_checksum_unknown_before_decode(tmp_path, capsys, monkeypatch, name):
    source = tmp_path / "data.parquet"
    pq.write_table(pa.table({"id": [1]}), source)
    def fail(*args, **kwargs):
        raise AssertionError("invalid selections must not decode")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    assert main(["validate", str(source), "--output", str(source), *options([name])]) == 2
    assert "unknown top-level row checksum columns" in capsys.readouterr().err


@pytest.mark.parametrize("side", ["input", "output"])
def test_row_checksum_budgets_before_decode(tmp_path, capsys, monkeypatch, side):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    pq.write_table(pa.table({"id": [1, 2] if side == "input" else [1]}), source)
    pq.write_table(pa.table({"id": [1, 2]}), output)
    def fail(*args, **kwargs):
        raise AssertionError("budget must precede decoding")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    assert main(["validate", str(source), "--output", str(output), *options(["id"]), "--max-rows", "1"]) == 2
    assert side + " rows" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["missing", "type", "nested", "order", "duplicate"])
def test_row_checksum_incompatible_artifact_invalidates_entire_digest(tmp_path, capsys, kind):
    source, output = tmp_path / "input.parquet", tmp_path / "output"
    output.mkdir()
    table = pa.table({"id": [1], "p": pa.array([None], pa.struct([("x", pa.int64()), ("y", pa.int64())]))})
    pq.write_table(table, source)
    pq.write_table(table, output / "a.parquet")
    if kind == "missing":
        bad = table.drop(["p"])
    elif kind == "type":
        bad = table.set_column(1, "p", pa.array(["bad"]))
    elif kind == "nested":
        bad = table.set_column(1, "p", pa.array([None], pa.struct([("x", pa.int64())])))
    elif kind == "order":
        bad = table.set_column(1, "p", pa.array([None], pa.struct([("y", pa.int64()), ("x", pa.int64())])))
    else:
        bad = table.set_column(1, "p", pa.StructArray.from_arrays([pa.array([1]), pa.array([2])], names=["x", "x"]))
    # Even an empty bad file invalidates the dataset-wide selected tuple digest.
    pq.write_table(bad.slice(0, 0), output / "z.parquet")
    code, report = validate(source, output, capsys, *options(["id", "p"]), *options(["id"], "--checksum-column"))
    assert code == 1 and not report["checks"]["schema"]["ok"]
    assert report["actual"]["row_checksum"] is None
    assert not report["checks"]["row_checksum"]["ok"]
    assert report["checks"]["row_checksum"]["expected"] == report["expected"]["row_checksum"]
    assert report["checks"]["row_checksum"]["actual"] is None
    assert report["checks"]["row_checksum"]["unavailable"]
    assert report["checks"]["checksums"]["columns"]["id"]["ok"]
    # An unselected schema defect still fails schema but doesn't poison the id tuple.
    code, report = validate(source, output, capsys, *options(["id"]))
    assert code == 1 and report["checks"]["row_checksum"]["ok"]


def test_row_checksum_shared_batches_planned_hive_missing_nested(tmp_path, capsys, monkeypatch):
    import datawrench.parquet_unify.cli as cli
    source = tmp_path / "input"
    p = source / "k=001"
    p.mkdir(parents=True)
    pq.write_table(pa.table({"id": pa.array([1, 2], pa.int32()),
                             "p": pa.array([{"d": 2932897}, None], pa.struct([("d", pa.date32())])),
                             "items": pa.array([[-2147483648, None], []], pa.list_(pa.date32())),
                             "x": [1, None]}), p / "a.parquet", row_group_size=2)
    p = source / "k=__HIVE_DEFAULT_PARTITION__"
    p.mkdir()
    pq.write_table(pa.table({"id": pa.array([3], pa.int64()), "x": ["001"],
                             "p": pa.array([{"label": "extra"}], pa.struct([("label", pa.string())]))}), p / "b.parquet")
    p = source / "plain"
    p.mkdir()
    pq.write_table(pa.table({"id": [4]}), p / "c.parquet")
    schema = pa.schema([("id", pa.int64()), ("p", pa.struct([("d", pa.date32()), ("label", pa.string())])),
                        ("items", pa.list_(pa.date32())), ("x", pa.string()), ("k", pa.string())])
    output = tmp_path / "output.parquet"
    rows = [{"id": 1, "p": {"d": 2932897, "label": None}, "items": [-2147483648, None], "x": "1", "k": "001"},
            {"id": 2, "p": None, "items": [], "x": None, "k": "001"},
            {"id": 3, "p": {"d": None, "label": "extra"}, "items": None, "x": "001", "k": None},
            {"id": 4, "p": None, "items": None, "x": None, "k": None}]
    table = pa.Table.from_pylist(list(reversed(rows)), schema=schema)
    pq.write_table(table, output)
    original = pq.ParquetFile.iter_batches
    sizes, passes = [], []
    def batches(reader, *args, **kwargs):
        assert kwargs["use_threads"] is False
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
    args = ["--hive", "--policy", "stringify-conflicts", "--batch-size", "1",
            "--null-column", "p", *options(schema.names), *options(["id", "p"], "--checksum-column")]
    code, report = validate(source, output, capsys, *args)
    assert code == 0 and report["checks"]["row_checksum"]["ok"]
    assert report["actual"]["null_counts"] == {"p": 2}
    assert max(sizes) == 1 and len(passes) == 4  # One shared decoding pass for all three metrics per file.
    # Reassociate nested values only: column multisets still equal, tuples differ.
    pq.write_table(table.set_column(1, "p", table["p"].take(pa.array([1, 0, 2, 3]))), output)
    code, report = validate(source, output, capsys, *args)
    assert code == 1 and report["checks"]["checksums"]["ok"] and not report["checks"]["row_checksum"]["ok"]


def test_row_checksum_empty_carriers_and_canonical_values(tmp_path, capsys):
    from decimal import Decimal
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    table = pa.table({"items": pa.array([[1, None], [], None], pa.list_(pa.int64())),
                      "text": ["é", None, ""], "f": [float("nan"), -0.0, None],
                      "d": pa.array([Decimal("1.20"), None, Decimal("0.00")], pa.decimal128(6, 2)),
                      "b": [b"\x00", b"", None], "n": pa.nulls(3)})
    carriers = table.schema.set(0, pa.field("items", pa.large_list(pa.int64()))).set(1, pa.field("text", pa.large_string()))
    for empty in [True, False]:
        data = table.slice(0, 0) if empty else table
        pq.write_table(data, source)
        other = data.cast(carriers)
        if not empty:
            other = other.set_column(2, "f", pa.array([float("nan"), 0.0, None]))
        pq.write_table(other, output)
        code, report = validate(source, output, capsys, *options(table.column_names), "--batch-size", "1")
        assert code == 0 and report["checks"]["row_checksum"]["ok"]
        if empty:
            assert report["expected"]["row_checksum"] == golden([], [])
