"""Per-file selected digests use planned types and retain independent eligibility."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main
from datawrench.parquet_unify.data import Summary


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(data, path, row_group_size=1)
    return path


def table(values):
    return pa.table({"id": pa.array(values, pa.int64())})


def digest(data, target=None):
    summary = Summary(target if target is not None else data.schema)
    summary.add(data)
    return summary.report()["checksums"][data.column_names[0]]


def records(items):
    return sorted([{"file": str(p), "checksum": value} for p, value in items], key=lambda r: r["file"])


def validate(source, output, capsys, *options):
    code = main(["validate", str(source), "--output", str(output), *options])
    captured = capsys.readouterr()
    return code, captured


@pytest.mark.parametrize("case", ["matching", "changed", "repartitioned", "all_empty"])
def test_checksum_file_evidence_exact_contract(tmp_path, capsys, monkeypatch, case):
    source, output = tmp_path / "input", tmp_path / "output"
    a, b = ([], []) if case == "all_empty" else ([1, 2], [3])
    aa, bb = ([3, 1], [2]) if case == "repartitioned" else (a, [4] if case == "changed" else b)
    # Absolute string ordering differs from pathlib's component ordering.
    paths = [write(source / "a.parquet", table(a)), write(source / "a" / "z.parquet", table(b)),
             write(output / "a.parquet", table(aa)), write(output / "a" / "z.parquet", table(bb))]
    for root in [source, output]:
        for name in ["_metadata", "_common_metadata"]:
            (root / name).write_text("ignored")
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
              for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.chdir(tmp_path)
    code, captured = validate("input", "output", capsys, "--checksum-column", "id", "--checksum-column", "id",
                              "--null-column", "id", "--row-checksum-column", "id", "--batch-size", "1")
    assert code == (1 if case == "changed" else 0) and not captured.err
    report = json.loads(captured.out)
    check = report["checks"]["checksums"]["columns"]["id"]
    assert set(check) == {"ok", "expected", "actual", "unavailable", "expected_by_file", "actual_by_file"}
    assert list(report["checks"]["checksums"]["columns"]) == ["id"]
    assert check["expected_by_file"] == records(zip(paths[:2], [digest(table(a)), digest(table(b))]))
    assert check["actual_by_file"] == records(zip(paths[2:], [digest(table(aa)), digest(table(bb))]))
    assert check["expected"] == digest(table(a + b))
    assert check["actual"] == digest(table(aa + bb))
    assert check["unavailable"] == []
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before}


@pytest.mark.parametrize("empty", [True, False])
@pytest.mark.parametrize("kind", ["missing", "type", "nested"])
def test_invalid_files_do_not_poison_healthy_file_evidence(tmp_path, capsys, kind, empty):
    source, output = tmp_path / "input.parquet", tmp_path / "output"
    dtype = pa.struct([("x", pa.int64())]) if kind == "nested" else pa.int64()
    data = table([1]).append_column("value", pa.array([None], dtype))
    write(source, data)
    bad = data.drop(["value"]) if kind == "missing" else data.set_column(
        1, "value", pa.array([None], pa.struct([("y", pa.int64())]) if kind == "nested" else pa.string()))
    # Both before and after healthy files, so a cumulative unknown must not hide them.
    bad_paths = [write(output / name, bad.slice(0, 0) if empty else bad) for name in ["0.parquet", "z.parquet"]]
    good = write(output / "a.parquet", data)
    good_empty = write(output / "b.parquet", data.slice(0, 0))
    code, captured = validate(source, output, capsys, "--checksum-column", "value", "--checksum-column", "id",
                              "--checksum-column", "value", "--row-checksum-column", "id",
                              "--ordered-row-checksum-column", "id", "--null-column", "value", "--batch-size", "1")
    assert code == 1 and not captured.err
    report = json.loads(captured.out)
    checks = report["checks"]["checksums"]["columns"]
    assert list(checks) == ["value", "id"]
    check = checks["value"]
    assert check["expected_by_file"] == records([(source, digest(data.select(["value"])))])
    assert check["actual_by_file"] == records([
        *[(p, None) for p in bad_paths], (good, digest(data.select(["value"]))),
        (good_empty, digest(data.select(["value"]).slice(0, 0)))])
    assert check["actual"] is None
    assert check["unavailable"] == [{"column": "value", **i} for i in report["checks"]["schema"]["issues"]]
    assert checks["id"]["actual"] is not None and checks["id"]["unavailable"] == []
    assert report["checks"]["row_checksum"]["actual"] is not None
    assert report["checks"]["ordered_row_checksum"]["actual"] is not None


@pytest.mark.parametrize("name,left,right,target,policy", [
    ("a.b", pa.array([1, None], pa.int32()), pa.array([3], pa.int64()), pa.int64(), "widen"),
    ("^x$", pa.array([True, None]), pa.array(["001"]), pa.string(), "stringify-conflicts"),
    ("p/~", pa.array([[{"x": 2}], None], pa.list_(pa.struct([("x", pa.int32())]))),
     pa.array([[{"y": "kept"}]], pa.list_(pa.struct([("y", pa.string())]))),
     pa.list_(pa.struct([("x", pa.int32()), ("y", pa.string())])), "widen"),
    ("[]", pa.array([None, []], pa.list_(pa.int64())), None, pa.list_(pa.int64()), "widen"),
    ("*", pa.array([None, None], pa.null()), None, pa.null(), "widen"),
    ("float", pa.array([float('nan'), -0.0]), pa.array([0.0]), pa.float64(), "widen"),
])
def test_file_evidence_uses_canonical_planned_types(tmp_path, capsys, name, left, right, target, policy):
    from datawrench.parquet_unify.data import normalize_table
    source, output = tmp_path / "input", tmp_path / "output"
    first = table([1, 2]).append_column(name, left)
    second = table([3]) if right is None else table([3]).append_column(name, right)
    a, b = write(source / "a", first), write(source / "b", second)
    schema = pa.schema([("id", pa.int64()), (name, target)])
    aa, bb = normalize_table(first, schema), normalize_table(second, schema)
    oa, ob = write(output / "a", aa), write(output / "b", bb)
    code, captured = validate(source, output, capsys, "--policy", policy, "--checksum-column", name, "--batch-size", "1")
    assert code == 0 and not captured.err
    check = json.loads(captured.out)["checks"]["checksums"]["columns"][name]
    expected = [digest(t.select([name])) for t in [aa, bb]]
    assert check["expected_by_file"] == records(zip([a, b], expected))
    assert check["actual_by_file"] == records(zip([oa, ob], expected))
    assert check["expected"] == digest(pa.concat_tables([aa, bb]).select([name])) == check["actual"]


def test_hive_skip_single_shared_bounded_pass(tmp_path, capsys, monkeypatch):
    source, output = tmp_path / "input", tmp_path / "output"
    files = [write(source / "k=001" / "data", table([1, 2])),
             write(source / "k=__HIVE_DEFAULT_PARTITION__" / "data", table([3])),
             write(source / "no-key" / "data", table([4])), write(source / "empty" / "data", table([]))]
    data = table([1, 2, 3, 4]).append_column("k", pa.array(["001", "001", None, None]))
    out = write(output / "k=not-inferred" / "materialized", data)
    bad = source / "bad"; bad.write_text("unreadable")
    (source / "_metadata").write_text("ignored")
    (output / "_common_metadata").write_text("ignored")
    opened, passes = [], []
    original_init, original_batches = pq.ParquetFile.__init__, pq.ParquetFile.iter_batches

    def init(reader, path, *args, **kwargs):
        opened.append(str(path)); original_init(reader, path, *args, **kwargs)

    def batches(reader, *args, **kwargs):
        assert kwargs == {"batch_size": 1, "use_threads": False}
        passes.append(reader)
        return original_batches(reader, *args, **kwargs)

    def fail(*args, **kwargs):
        raise AssertionError("full table/row group materialization forbidden")

    monkeypatch.setattr(pq.ParquetFile, "__init__", init)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    monkeypatch.setattr(pq.ParquetFile, "read", fail)
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", fail)
    monkeypatch.setattr(pq, "read_table", fail)
    code, captured = validate(source, output, capsys, "--hive", "--skip-unreadable", "--checksum-column", "k",
                              "--null-column", "k", "--row-checksum-column", "id",
                              "--ordered-row-checksum-column", "id", "--batch-size", "1")
    assert code == 0 and "warning:" in captured.err
    assert len(opened) == 11 and len(passes) == 5  # six scans, five shared metric passes
    report = json.loads(captured.out)
    check = report["checks"]["checksums"]["columns"]["k"]
    values = [["001", "001"], [None], [None], []]
    assert check["expected_by_file"] == records(zip(files, [digest(pa.table({"k": pa.array(v, pa.string())})) for v in values]))
    assert check["actual_by_file"] == records([(out, digest(data.select(["k"])))])
    assert report["input"]["unreadable"][0]["file"] == str(bad)


@pytest.mark.parametrize("case", ["unknown", "unreadable", "input_budget", "output_budget", "no_selection"])
def test_checksum_evidence_preserves_guards(tmp_path, capsys, monkeypatch, case):
    source, output = write(tmp_path / "input", table([1, 2])), write(tmp_path / "output", table([1, 2]))
    options = ["--checksum-column", "id", "--max-rows", "0"]
    if case in ["unknown", "unreadable"]:
        output.write_text("invalid")
        if case == "unknown": options += ["--checksum-column", "ID"]
    elif case == "output_budget":
        write(source, table([]))
    elif case == "no_selection":
        options = []

    def fail(*args, **kwargs):
        raise AssertionError("must not decode")

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    code, captured = validate(source, output, capsys, *options)
    if case == "no_selection":
        assert code == 0 and not captured.err
        assert "checksums" not in json.loads(captured.out)["checks"]
    else:
        assert code == 2 and captured.out == ""
        assert {"unknown": "unknown top-level checksum columns", "unreadable": "output unreadable",
                "input_budget": "input rows 2 exceed", "output_budget": "output rows 2 exceed"}[case] in captured.err


def test_checksum_only_healthy_after_invalid_uses_one_pass(tmp_path, capsys, monkeypatch):
    source, output = tmp_path / "input", tmp_path / "output"
    write(source, table([1, 2]))
    bad = write(output / "0", pa.table({"other": pa.array([], pa.int64())}))
    good = write(output / "a", table([1, 2]))
    opened, passes = [], []
    original_init, original_batches = pq.ParquetFile.__init__, pq.ParquetFile.iter_batches

    def init(reader, path, *args, **kwargs):
        opened.append(str(path))
        original_init(reader, path, *args, **kwargs)

    def batches(reader, *args, **kwargs):
        assert kwargs == {"batch_size": 1, "use_threads": False}
        passes.append(reader)
        return original_batches(reader, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "__init__", init)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    code, captured = validate(source, output, capsys, "--checksum-column", "id", "--batch-size", "1")
    assert code == 1 and not captured.err
    check = json.loads(captured.out)["checks"]["checksums"]["columns"]["id"]
    assert check["actual_by_file"] == records([(bad, None), (good, digest(table([1, 2])))])
    assert check["actual"] is None
    assert len(opened) == 5 and len(passes) == 2  # three footer scans, two required metric passes
