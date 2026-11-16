"""Per-file planned tuple multisets localize value and schema failures."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main
from datawrench.parquet_unify.data import RowSummary, normalize_table


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(data, path, row_group_size=1)
    return path


def table(ids, notes):
    return pa.table({"id": pa.array(ids, pa.int64()), "note": pa.array(notes, pa.string())})


def digest(data, target=None):
    summary = RowSummary(target if target is not None else data.schema)
    summary.add(data)
    return summary.digest()


def golden(rows):
    schema = [{"name": "id", "type": "int64"}, {"name": "note", "type": "string"}]
    hashes = [int.from_bytes(hashlib.sha256(json.dumps(
        ["parquet-unify-row-v1", schema, row], ensure_ascii=True, separators=(",", ":")
    ).encode()).digest(), "big") for row in rows]
    a, b = sum(hashes) % (1 << 256), sum(h*h for h in hashes) % (1 << 256)
    return hashlib.sha256(f"{len(rows)}:{a:064x}:{b:064x}".encode()).hexdigest()


def records(items):
    return sorted([{"file": str(p), "checksum": value} for p, value in items], key=lambda r: r["file"])


def validate(source, output, capsys, names=("id", "note"), *options):
    args = [arg for name in names for arg in ("--row-checksum-column", name)]
    code = main(["validate", str(source), "--output", str(output), *args, *options])
    captured = capsys.readouterr()
    return code, captured


@pytest.mark.parametrize("case", ["matching", "reassociated", "repartitioned", "empty"])
def test_tuple_file_exact_contract(tmp_path, capsys, monkeypatch, case):
    source, output = tmp_path / "input", tmp_path / "output"
    a, b = table([1, 2], ["a", "b"]), table([3, 4], ["c", "d"])
    aa, bb = a, b
    if case == "reassociated": bb = table([3, 4], ["d", "c"])
    elif case == "repartitioned": aa, bb = b.take(pa.array([1, 0])), a
    elif case == "empty": a = b = aa = bb = a.slice(0, 0)
    # Compare absolute strings, not pathlib component order.
    paths = [write(source / "a.parquet", a), write(source / "a" / "z.parquet", b),
             write(output / "a.parquet", aa), write(output / "a" / "z.parquet", bb)]
    for root in [source, output]:
        for name in ["_metadata", "_common_metadata"]: (root / name).write_text("ignored")
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
              for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.chdir(tmp_path)
    code, captured = validate("input", "output", capsys, ("id", "note", "id"),
                              "--checksum-column", "id", "--checksum-column", "note", "--batch-size", "1")
    assert code == (1 if case == "reassociated" else 0) and not captured.err
    report = json.loads(captured.out)
    check = report["checks"]["row_checksum"]
    assert set(check) == {"ok", "expected", "actual", "unavailable", "expected_by_file", "actual_by_file"}
    assert report["scope"]["row_checksum_columns"] == ["id", "note"]
    assert report["checks"]["checksums"]["ok"]
    values = [golden([[r['id'], r['note']] for r in t.to_pylist()]) for t in [a, b, aa, bb]]
    assert check["expected_by_file"] == records(zip(paths[:2], values[:2]))
    assert check["actual_by_file"] == records(zip(paths[2:], values[2:]))
    assert check["expected"] == golden([[r['id'], r['note']] for t in [a, b] for r in t.to_pylist()])
    assert check["actual"] == golden([[r['id'], r['note']] for t in [aa, bb] for r in t.to_pylist()])
    assert check["unavailable"] == []
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before}


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("kind", ["missing", "type", "nested", "order", "duplicate"])
def test_tuple_invalid_files_keep_healthy_evidence(tmp_path, capsys, kind, empty):
    source, output = tmp_path / "input", tmp_path / "output"
    fields = [("x", pa.int64()), ("y", pa.string())]
    data = table([1], ["a"]).append_column("p/~", pa.array([None], pa.list_(pa.struct(fields))))
    write(source, data)
    if kind == "missing": bad = data.drop(["p/~"])
    elif kind == "type": bad = data.set_column(2, "p/~", pa.array(["bad"]))
    else:
        changed = {"nested": [fields[0], ("z", pa.int64())], "order": fields[::-1],
                   "duplicate": [fields[0], *fields]}[kind]
        bad = data.set_column(2, "p/~", pa.array([None], pa.list_(pa.struct(changed))))
    bad_paths = [write(output / name, bad.slice(0, 0) if empty else bad) for name in ["0", "z"]]
    good, good_empty = write(output / "a", data), write(output / "b", data.slice(0, 0))
    names = ["p/~", "id"]
    code, captured = validate(source, output, capsys, [*names, "p/~"], "--null-column", "p/~",
                              "--checksum-column", "id", "--ordered-row-checksum-column", "id", "--batch-size", "1")
    assert code == 1 and not captured.err
    report = json.loads(captured.out); check = report["checks"]["row_checksum"]
    assert check["actual"] is None
    assert check["actual_by_file"] == records([*[(p, None) for p in bad_paths],
        (good, digest(data.select(names))), (good_empty, digest(data.select(names).slice(0, 0)))])
    assert check["expected_by_file"] == records([(source, digest(data.select(names)))])
    assert check["unavailable"] == [{"column": "p/~", **i} for i in report["checks"]["schema"]["issues"]]
    assert report["checks"]["checksums"]["columns"]["id"]["actual"] is not None
    assert report["checks"]["ordered_row_checksum"]["actual"] is not None


@pytest.mark.parametrize("kind", ["root_order", "unselected"])
def test_tuple_eligible_despite_unselected_issues(tmp_path, capsys, kind):
    data = table([1], ["a"])
    source = write(tmp_path / "input", data)
    other = data.select(["note", "id"]) if kind == "root_order" else data.drop(["note"])
    output = write(tmp_path / "output", other)
    code, captured = validate(source, output, capsys, ["id"])
    assert code == 1 and not captured.err
    check = json.loads(captured.out)["checks"]["row_checksum"]
    assert check["actual_by_file"] == records([(output, digest(data.select(["id"])))])
    assert check["ok"] and check["unavailable"] == []


@pytest.mark.parametrize("policy", ["widen", "stringify-conflicts"])
def test_tuple_planned_types_and_literal_order(tmp_path, capsys, policy):
    source, output = tmp_path / "input", tmp_path / "output"
    dtype = pa.int64() if policy == "widen" else pa.string()
    a = pa.table({"a.b": pa.array([1, None], pa.int32()), "*": ["a", "b"]})
    b = pa.table({"a.b": pa.array([3] if policy == "widen" else ["003"], dtype)})
    schema = pa.schema([("a.b", dtype), ("*", pa.string())])
    aa, bb = normalize_table(a, schema), normalize_table(b, schema)
    paths = [write(source / "a", a), write(source / "b", b), write(output / "a", aa), write(output / "b", bb)]
    for names in [["*", "a.b"], ["a.b", "*"]]:
        code, captured = validate(source, output, capsys, [*names, names[0]], "--policy", policy, "--batch-size", "1")
        assert code == 0 and not captured.err
        check = json.loads(captured.out)["checks"]["row_checksum"]
        values = [digest(t.select(names)) for t in [aa, bb]]
        assert check["expected_by_file"] == records(zip(paths[:2], values))
        assert check["actual_by_file"] == records(zip(paths[2:], values))
        assert check["expected"] == check["actual"] == digest(pa.concat_tables([aa, bb]).select(names))


def test_tuple_hive_skip_one_shared_pass(tmp_path, capsys, monkeypatch):
    source, output = tmp_path / "input", tmp_path / "output"
    files = [write(source / "k=001" / "data", table([1, 2], ["a", "b"])),
             write(source / "k=__HIVE_DEFAULT_PARTITION__" / "data", table([3], ["c"])),
             write(source / "no-key" / "data", table([4], ["d"])),
             write(source / "empty" / "data", table([], []))]
    data = table([1, 2, 3, 4], ["a", "b", "c", "d"]).append_column("k", pa.array(["001", "001", None, None]))
    out = write(output / "k=not-inferred" / "data", data)
    (source / "bad").write_text("unreadable")
    (source / "_metadata").write_text("ignored"); (output / "_common_metadata").write_text("ignored")
    opened, passes = [], []
    original_init, original_batches = pq.ParquetFile.__init__, pq.ParquetFile.iter_batches
    def init(reader, path, *args, **kwargs):
        opened.append(str(path)); original_init(reader, path, *args, **kwargs)
    def batches(reader, *args, **kwargs):
        assert kwargs == {"batch_size": 1, "use_threads": False}
        passes.append(reader)
        for batch in original_batches(reader, *args, **kwargs):
            assert batch.num_rows <= 1
            yield batch
    def fail(*args, **kwargs): raise AssertionError("full table read forbidden")
    monkeypatch.setattr(pq.ParquetFile, "__init__", init)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    for name in ["read", "read_row_group"]: monkeypatch.setattr(pq.ParquetFile, name, fail)
    monkeypatch.setattr(pq, "read_table", fail)
    code, captured = validate(source, output, capsys, ["k", "id"], "--hive", "--skip-unreadable",
        "--checksum-column", "note", "--null-column", "k", "--ordered-row-checksum-column", "id", "--batch-size", "1")
    assert code == 0 and "warning:" in captured.err
    assert len(opened) == 11 and len(passes) == 5
    check = json.loads(captured.out)["checks"]["row_checksum"]
    parts = [data.slice(0, 2), data.slice(2, 1), data.slice(3, 1), data.slice(0, 0)]
    assert check["expected_by_file"] == records(zip(files, [digest(t.select(["k", "id"])) for t in parts]))
    assert check["actual_by_file"] == records([(out, digest(data.select(["k", "id"])))])


def test_tuple_only_healthy_after_invalid_one_pass(tmp_path, capsys, monkeypatch):
    data = table([1, 2], ["a", "b"])
    source = write(tmp_path / "input", data)
    output = tmp_path / "output"
    bad = write(output / "0", data.drop(["note"]).slice(0, 0))
    good = write(output / "a", data)
    passes = []
    original = pq.ParquetFile.iter_batches
    def batches(reader, *args, **kwargs):
        assert kwargs == {"batch_size": 1, "use_threads": False}
        passes.append(reader)
        return original(reader, *args, **kwargs)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    code, captured = validate(source, output, capsys, ["id", "note"], "--batch-size", "1")
    assert code == 1 and not captured.err
    check = json.loads(captured.out)["checks"]["row_checksum"]
    assert check["actual_by_file"] == records([(bad, None), (good, digest(data))])
    assert len(passes) == 2


@pytest.mark.parametrize("case", ["unknown", "unreadable", "input_budget", "output_budget", "no_selection"])
def test_tuple_evidence_preserves_guards(tmp_path, capsys, monkeypatch, case):
    data = table([1, 2], ["a", "b"])
    source, output = write(tmp_path / "input", data), write(tmp_path / "output", data)
    names, extra = ["id"], ["--max-rows", "0"]
    if case in ["unknown", "unreadable"]:
        output.write_text("invalid")
        if case == "unknown": names += ["ID"]
    elif case == "output_budget": write(source, data.slice(0, 0))
    elif case == "no_selection": names, extra = [], []
    def fail(*args, **kwargs): raise AssertionError("must not decode")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    code, captured = validate(source, output, capsys, names, *extra)
    if case == "no_selection":
        assert code == 0 and not captured.err
        assert "row_checksum" not in json.loads(captured.out)["checks"]
    else:
        assert code == 2 and not captured.out
        assert {"unknown": "unknown top-level row checksum columns", "unreadable": "output unreadable",
            "input_budget": "input rows 2 exceed", "output_budget": "output rows 2 exceed"}[case] in captured.err
