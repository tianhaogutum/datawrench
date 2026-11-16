"""Footer row evidence describes each scoped file without pairing source/output."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main


def table(values):
    return pa.table({"id": pa.array(values, pa.int64())})


def validate(source, output, capsys, *options):
    code = main(["validate", str(source), "--output", str(output), *options])
    captured = capsys.readouterr()
    assert not captured.err
    return code, json.loads(captured.out)


def assert_rows(report, expected, actual):
    expected = sorted([{"file": str(p), "rows": n} for p, n in expected], key=lambda r: r["file"])
    actual = sorted([{"file": str(p), "rows": n} for p, n in actual], key=lambda r: r["file"])
    er, ar = sum(r["rows"] for r in expected), sum(r["rows"] for r in actual)
    assert report["checks"]["rows"] == {"ok": er == ar, "expected": er, "actual": ar,
                                        "expected_by_file": expected, "actual_by_file": actual}
    for key, records in [("input", expected), ("output", actual)]:
        assert sorted(report[key]["files"]) == [r["file"] for r in records]
    assert report["expected"]["rows"] == er and report["actual"]["rows"] == ar


@pytest.mark.parametrize("case", ["mismatch", "match", "all_empty", "single_file", "nested_order"])
def test_exact_row_evidence_is_footer_only(tmp_path, capsys, monkeypatch, case):
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    expected, actual = [], []
    empty = case == "all_empty"
    for root, contents, records in [
        (source, [("a/z.parquet" if case == "nested_order" else "b.parquet", [] if empty else [3]), ("a.parquet", [] if empty else [1, 2])], expected),
        (output, [("z.parquet", [3] if case == "match" else []),
                  ("a.parquet", [] if empty else [1, 2])], actual),
    ]:
        for name, values in contents:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table(values), path, row_group_size=1)
            records.append((path, len(values)))
        # Standard sidecars are not data contributions, even when not Parquet.
        for name in ["_metadata", "_common_metadata"]:
            (root / name).write_text("ignored")
    if case == "single_file":
        source, output = source / "a.parquet", output / "a.parquet"
        expected, actual = [(source, 2)], [(output, 2)]
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    opened = []
    original_init = pq.ParquetFile.__init__

    def init(reader, path, *args, **kwargs):
        opened.append(str(path))
        original_init(reader, path, *args, **kwargs)

    def fail(*args, **kwargs):
        raise AssertionError("row contributions must use already scanned footers only")

    monkeypatch.setattr(pq.ParquetFile, "__init__", init)
    for method in ["iter_batches", "read", "read_row_group"]:
        monkeypatch.setattr(pq.ParquetFile, method, fail)
    monkeypatch.chdir(tmp_path)  # Relative roots must still produce absolute evidence.
    code, report = validate(source.relative_to(tmp_path), output.relative_to(tmp_path), capsys)
    assert code == (1 if case in ["mismatch", "nested_order"] else 0)
    assert_rows(report, expected, actual)
    assert opened == [str(p) for p, _ in sorted(expected)] + [str(p) for p, _ in sorted(actual)]
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}


def test_rows_scope_hive_and_skipped_input(tmp_path, capsys):
    source, output = tmp_path / "input", tmp_path / "output"
    output.mkdir()
    expected = []
    for key, values in [("001", [1, 2]), ("__HIVE_DEFAULT_PARTITION__", [])]:
        part = source / ("k=" + key)
        part.mkdir(parents=True)
        path = part / "extensionless"
        pq.write_table(table(values), path)
        expected.append((path, len(values)))
    bad = source / "bad.txt"
    bad.write_text("unreadable")
    artifact = output / "materialized.parquet"
    pq.write_table(pa.table({"id": pa.array([1, 2], pa.int64()), "k": ["001", "001"]}), artifact)
    # Undeclared sibling must not enter either scoped list.
    pq.write_table(table([9, 9, 9]), tmp_path / "sibling.parquet")
    args = ["validate", str(source), "--output", str(output), "--hive"]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "unreadable files" in captured.err
    assert main(args + ["--skip-unreadable"]) == 0
    captured = capsys.readouterr()
    assert "warning:" in captured.err
    report = json.loads(captured.out)
    assert_rows(report, expected, [(artifact, 2)])
    assert report["input"]["hive"] is True
    assert [r["file"] for r in report["input"]["unreadable"]] == [str(bad)]
    (output / "bad.txt").write_text("never skipped")
    assert main(args + ["--skip-unreadable"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "output unreadable" in captured.err


@pytest.mark.parametrize("side", ["input", "output"])
def test_no_readable_rows_is_an_error_not_a_zero_total(tmp_path, capsys, side):
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    pq.write_table(table([]), (output if side == "input" else source) / "empty.parquet")
    args = ["validate", str(source), "--output", str(output), "--skip-unreadable"]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "empty input" in captured.err
    (source if side == "input" else output).joinpath("bad.txt").write_text("bad")
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("no readable" if side == "input" else "output unreadable") in captured.err


def test_evidence_keeps_selected_metrics_in_one_bounded_pass(tmp_path, capsys, monkeypatch):
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    expected, actual = [], []
    for root, records in [(source, expected), (output, actual)]:
        for name, values in [("z.parquet", []), ("a.parquet", [1, None, 2])]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table(values), path, row_group_size=2)
            records.append((path, len(values)))
    original = pq.ParquetFile.iter_batches
    passes, sizes = [], []

    def batches(reader, *args, **kwargs):
        assert kwargs == {"batch_size": 1, "use_threads": False}
        passes.append(reader)
        for batch in original(reader, *args, **kwargs):
            sizes.append(batch.num_rows)
            yield batch

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    code, report = validate(source, output, capsys, "--null-column", "id", "--checksum-column", "id",
                            "--row-checksum-column", "id", "--batch-size", "1", "--max-rows", "3")
    assert code == 0 and all(c["ok"] for c in report["checks"].values())
    assert_rows(report, expected, actual)
    assert len(passes) == 4 and max(sizes) == 1
    assert report["expected"]["null_counts"] == report["actual"]["null_counts"] == {"id": 1}
    assert report["expected"]["checksums"] == report["actual"]["checksums"]
    assert report["expected"]["row_checksum"] == report["actual"]["row_checksum"]
