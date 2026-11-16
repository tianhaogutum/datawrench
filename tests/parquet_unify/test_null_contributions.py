"""Selected null evidence describes independent files, including unknown empty outputs."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main


def table(ids, notes=None):
    result = pa.table({"id": pa.array(ids, pa.int64())})
    if notes is not None:
        result = result.append_column("note", pa.array(notes, pa.string()))
    return result


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(data, path, row_group_size=1)
    return path


def validate(source, output, capsys, *options):
    code = main(["validate", str(source), "--output", str(output), *options])
    captured = capsys.readouterr()
    return code, captured


def records(items):
    return sorted([{"file": str(p), "nulls": n} for p, n in items], key=lambda r: r["file"])


def assert_evidence(check, expected, actual):
    assert check["expected_by_file"] == records(expected)
    assert check["actual_by_file"] == records(actual)
    assert check["expected"] == sum(n for _, n in expected)
    assert check["actual"] == (None if any(n is None for _, n in actual) else sum(n for _, n in actual))


@pytest.mark.parametrize("case", ["mismatch", "correct", "missing_empty", "missing_nonempty", "all_empty"])
def test_selected_null_contributions_exact_contract(tmp_path, capsys, monkeypatch, case):
    source, output = tmp_path / "input", tmp_path / "output"
    empty = case == "all_empty"
    # Path component sorting differs from absolute string sorting here.
    a = write(source / "a.parquet", table([] if empty else [1, 2], [] if empty else ["kept", None]))
    b = write(source / "a" / "z.parquet", table([] if empty else [3]))
    oa = write(output / "a.parquet", table([] if empty else [1, 2], [] if empty else ["kept", "filled" if case == "mismatch" else None]))
    ob = write(output / "a" / "z.parquet", table([] if empty else [3], [] if empty else [None]))
    actual = [(oa, 0 if case == "mismatch" or empty else 1), (ob, 0 if empty else 1)]
    if case.startswith("missing"):
        for name in ["0.parquet", "z.parquet"]:
            path = write(output / name, table([] if case == "missing_empty" else [4]))
            actual.append((path, None))
    for root in [source, output]:
        for name in ["_metadata", "_common_metadata"]:
            (root / name).write_text("ignored sidecar")
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    monkeypatch.chdir(tmp_path)
    code, captured = validate("input", "output", capsys, "--null-column", "note", "--null-column", "id",
                              "--null-column", "note", "--batch-size", "1")
    assert not captured.err and code == (0 if case in ["correct", "all_empty"] else 1)
    report = json.loads(captured.out)
    checks = report["checks"]["null_counts"]["columns"]
    assert list(checks) == ["note", "id"] and report["scope"]["null_columns"] == ["note", "id"]
    assert set(checks["note"]) == {"ok", "expected", "actual", "unavailable", "expected_by_file", "actual_by_file"}
    assert_evidence(checks["note"], [(a, 0 if empty else 1), (b, 0 if empty else 1)], actual)
    assert_evidence(checks["id"], [(a, 0), (b, 0)], [(p, 0) for p, _ in actual])
    assert checks["note"]["unavailable"] == [
        {"column": "note", **i} for i in report["checks"]["schema"]["issues"]
        if i["path_parts"] == ["note"] and i["reason"] == "missing_field"]
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}


@pytest.mark.parametrize("name,dtype,values", [
    ("a.b", pa.string(), [None, "kept"]),
    ("p/~", pa.struct([("x", pa.int64())]), [None, {"x": None}]),
    ("[]", pa.list_(pa.int64()), [None, []]),
    ("*", pa.null(), [None, None]),
])
def test_single_file_literal_parent_evidence(tmp_path, capsys, name, dtype, values):
    data = pa.table({name: pa.array(values, dtype)})
    source, output = write(tmp_path / "input.parquet", data), write(tmp_path / "output.parquet", data)
    code, captured = validate(source, output, capsys, "--null-column", name, "--checksum-column", name,
                              "--row-checksum-column", name, "--ordered-row-checksum-column", name, "--batch-size", "1")
    assert code == 0 and not captured.err
    report = json.loads(captured.out)
    n = 2 if name == "*" else 1
    assert_evidence(report["checks"]["null_counts"]["columns"][name], [(source, n)], [(output, n)])
    assert all(c["ok"] for c in report["checks"].values())


def test_hive_missing_constants_skip_and_no_extra_decoding(tmp_path, capsys, monkeypatch):
    source, output = tmp_path / "input", tmp_path / "output"
    files = [write(source / "k=001" / "extensionless", table([1, 2])),
             write(source / "k=__HIVE_DEFAULT_PARTITION__" / "data", table([3])),
             write(source / "no-key" / "data", table([4])),
             write(source / "empty" / "data", table([]))]
    artifact = write(output / "materialized", table([1, 2, 3, 4]).append_column("k", pa.array(["001", "001", None, None])))
    bad = source / "bad.txt"
    bad.write_text("unreadable")
    (source / "_metadata").write_text("ignored")
    (output / "_common_metadata").write_text("ignored")
    opened, passes = [], []
    original_init, original_batches = pq.ParquetFile.__init__, pq.ParquetFile.iter_batches

    def init(reader, path, *args, **kwargs):
        opened.append(str(path))
        original_init(reader, path, *args, **kwargs)

    def batches(reader, *args, **kwargs):
        assert kwargs == {"batch_size": 1, "use_threads": False}
        passes.append(reader)
        return original_batches(reader, *args, **kwargs)

    def fail(*args, **kwargs):
        raise AssertionError("full-table reads forbidden")

    monkeypatch.setattr(pq.ParquetFile, "__init__", init)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    monkeypatch.setattr(pq.ParquetFile, "read", fail)
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", fail)
    monkeypatch.setattr(pq, "read_table", fail)
    # Selecting only Hive constants needs no input metric opens or passes.
    code, captured = validate(source, output, capsys, "--hive", "--skip-unreadable", "--null-column", "k", "--batch-size", "1")
    assert code == 0 and "warning:" in captured.err
    report = json.loads(captured.out)
    assert_evidence(report["checks"]["null_counts"]["columns"]["k"], list(zip(files, [0, 1, 1, 0])), [(artifact, 2)])
    assert len(opened) == 7 and len(passes) == 1  # Five input scans + output scan + output metrics.
    assert [r["file"] for r in report["input"]["unreadable"]] == [str(bad)]
    opened.clear(); passes.clear()
    # All four metric families share each file pass, with no extra evidence reads.
    code, captured = validate(source, output, capsys, "--hive", "--skip-unreadable", "--null-column", "k",
                              "--null-column", "id", "--checksum-column", "k", "--row-checksum-column", "k",
                              "--ordered-row-checksum-column", "k", "--batch-size", "1")
    assert code == 0
    assert len(opened) == 11 and len(passes) == 5
    assert_evidence(json.loads(captured.out)["checks"]["null_counts"]["columns"]["k"], list(zip(files, [0, 1, 1, 0])), [(artifact, 2)])


@pytest.mark.parametrize("case", ["unknown", "unreadable", "input_budget", "output_budget", "no_selection"])
def test_evidence_preserves_predecode_boundaries(tmp_path, capsys, monkeypatch, case):
    source = write(tmp_path / "input.parquet", table([1, 2], [None, "kept"]))
    output = write(tmp_path / "output.parquet", table([1, 2], [None, "kept"]))
    options = ["--null-column", "note", "--max-rows", "0"]
    if case in ["unknown", "unreadable"]:
        output.write_text("invalid parquet")
        if case == "unknown": options += ["--null-column", "unknown"]
    elif case == "output_budget":
        write(source, table([], []))
    elif case == "no_selection":
        options = []

    def fail(*args, **kwargs):
        raise AssertionError("predecode boundaries and no selection must not decode")

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    code, captured = validate(source, output, capsys, *options)
    if case == "no_selection":
        assert code == 0 and not captured.err
        assert json.loads(captured.out)["checks"]["null_counts"] == {"ok": True, "columns": {}}
    else:
        assert code == 2 and captured.out == ""
        assert {"unknown": "unknown top-level null columns", "unreadable": "output unreadable",
                "input_budget": "input rows 2 exceed", "output_budget": "output rows 2 exceed"}[case] in captured.err
