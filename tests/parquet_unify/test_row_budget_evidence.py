"""Budget errors carry only the refusing side's already scanned footer rows."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main


SELECTIONS = [[], ["--null-column", "id"], ["--checksum-column", "id"],
              ["--row-checksum-column", "id"], ["--ordered-row-checksum-column", "id"]]
SELECTIONS.append([arg for selection in SELECTIONS[1:] for arg in selection])


def write(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": pa.array(values, pa.int64())}), path, row_group_size=1)
    return {"file": str(path), "rows": len(values)}


def error(side, records, limit):
    records = sorted(records, key=lambda r: r["file"])
    return (f"error: {side} rows {sum(r['rows'] for r in records)} exceed max-rows {limit}; "
            "no column data decoded; rows_by_file="
            + json.dumps(records, ensure_ascii=True, separators=(",", ":")) + "\n")


def footer_only(monkeypatch):
    opened = []
    original = pq.ParquetFile.__init__

    def init(reader, path, *args, **kwargs):
        opened.append(str(path))
        original(reader, path, *args, **kwargs)

    def fail(*args, **kwargs):
        raise AssertionError("budget evidence must precede every data decode")

    monkeypatch.setattr(pq.ParquetFile, "__init__", init)
    for method in ["iter_batches", "read", "read_row_group"]:
        monkeypatch.setattr(pq.ParquetFile, method, fail)
    return opened


@pytest.mark.parametrize("selection", SELECTIONS)
@pytest.mark.parametrize("side", ["input", "output", "both"])
def test_budget_evidence_exact_footer_only(tmp_path, capsys, monkeypatch, side, selection):
    source, output = tmp_path / "input", tmp_path / "output"
    # Path-component traversal puts a/z before a.parquet; string order reverses them.
    expected = [write(source / "a.parquet", [1, 2]), write(source / "a/z.parquet", [3]),
                write(source / 'z-"é\\\n.parquet', [])]
    actual = [write(output / "a.parquet", [1, 2]),
              write(output / "b.parquet", [3] if side == "input" else [3, 4]),
              write(output / "z-empty", [])]
    for root in (source, output):
        for name in ("_metadata", "_common_metadata"):
            (root / name).write_text("ignored sidecar")
    write(tmp_path / "undeclared.parquet", [9] * 10)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    opened = footer_only(monkeypatch)
    monkeypatch.chdir(tmp_path)
    limit = 3 if side == "output" else 2
    assert main(["validate", "input", "--output", "output", *selection, "--max-rows", str(limit)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    refusing = "input" if side == "both" else side
    assert captured.err == error(refusing, expected if refusing == "input" else actual, limit)
    # Exactly the existing scan opens, in existing catalog order; no evidence rereads.
    assert opened == [str(p) for root in (source, output) for p in sorted(root.rglob("*"))
                      if p.is_file() and p.name not in ("_metadata", "_common_metadata")]
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}


@pytest.mark.parametrize("fmt", ["json", "text", "md"])
@pytest.mark.parametrize("side", ["input", "output"])
def test_single_file_zero_limit(tmp_path, capsys, monkeypatch, fmt, side):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    expected = write(source, [1] if side == "input" else [])
    actual = write(output, [1])
    opened = footer_only(monkeypatch)
    assert main(["validate", str(source), "--output", str(output), "--format", fmt, "--max-rows", "0"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == error(side, [expected if side == "input" else actual], 0)
    assert opened == [str(source), str(output)]


def test_skipped_input_hive_sidecars_and_output_refusal(tmp_path, capsys, monkeypatch):
    source, output = tmp_path / "input", tmp_path / "output.parquet"
    records = [write(source / "k=001/a.parquet", [1, 2]),
               write(source / "k=__HIVE_DEFAULT_PARTITION__/b.parquet", [])]
    bad = source / "bad.txt"
    bad.write_text("not parquet")
    for name in ("_metadata", "_common_metadata"):
        (source / name).write_text("not data")
    write(output, [1, 2, 3])
    opened = footer_only(monkeypatch)
    args = ["validate", str(source), "--output", str(output), "--hive", "--max-rows", "1"]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("error: unreadable files")
    assert "rows_by_file=" not in captured.err
    opened.clear()
    assert main(args + ["--skip-unreadable"]) == 2
    captured = capsys.readouterr()
    warning, diagnostic = captured.err.split("\n", 1)
    assert warning.startswith("warning: excluding unreadable files: ")
    assert [r["file"] for r in json.loads(warning.split(": ", 2)[2])] == [str(bad)]
    assert captured.out == "" and diagnostic == error("input", records, 1)
    assert opened == [str(bad), *[r["file"] for r in records], str(output)]
    # The same skip flag cannot turn unreadable output into budget evidence.
    output.write_text("bad output")
    assert main(args + ["--skip-unreadable"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "error: output unreadable files:" in captured.err
    assert "rows_by_file=" not in captured.err


@pytest.mark.parametrize("selection,label", [
    ("--null-column", "null"), ("--checksum-column", "checksum"),
    ("--row-checksum-column", "row checksum"), ("--ordered-row-checksum-column", "ordered row checksum")])
def test_unknown_name_precedes_output_scan_and_budget(tmp_path, capsys, monkeypatch, selection, label):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    write(source, [1, 2, 3])
    output.write_text("unreadable output")
    opened = footer_only(monkeypatch)
    assert main(["validate", str(source), "--output", str(output), selection, "ID", "--max-rows", "0"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == f"error: unknown top-level {label} columns: ['ID']\n"
    assert opened == [str(source)]
