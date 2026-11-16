"""Validate existing artifacts using real local Parquet, without running recipes."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.cli import main


@pytest.fixture
def artifacts(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    pq.write_table(pa.table({"id": pa.array([1, 2], pa.int32()),
                             "note": pa.array(["kept", None])}), source / "a.parquet")
    pq.write_table(pa.table({"id": pa.array([3], pa.int64())}), source / "b.parquet")
    good = pa.table({"id": pa.array([1, 2, 3], pa.int64()),
                     "note": pa.array(["kept", None, None])})
    paths = {}
    tables = {"good": good, "deleted": good.slice(0, 2),
              "type": good.set_column(0, "id", good["id"].cast(pa.int32())),
              "nulls": good.set_column(1, "note", pa.array(["kept", "filled", None])),
              "values": good.set_column(0, "id", pa.array([91, 92, 93], pa.int64()))}
    for name, table in tables.items():
        paths[name] = tmp_path / (name + ".parquet")
        pq.write_table(table, paths[name], row_group_size=2)
    return source, paths


def validate(source, artifact, capsys, *options):
    code = main(["validate", str(source), "--output", str(artifact), *options])
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


@pytest.mark.parametrize("artifact,failed", [("good", None), ("deleted", "rows"),
                                             ("type", "schema"), ("nulls", "null_counts")])
def test_correct_artifact_and_separate_corruption_diagnostics(artifacts, capsys, artifact, failed):
    source, paths = artifacts
    files = list(source.iterdir()) + list(paths.values())
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    code, report = validate(source, paths[artifact], capsys, "--null-column", "note", "--batch-size", "1")
    assert code == (1 if failed else 0)
    assert report["ok"] == (failed is None)
    assert report["expected"] == {"rows": 3, "schema": [{"name": "id", "type": "int64"},
                                                         {"name": "note", "type": "string"}],
                                   "null_counts": {"note": 2}}
    if failed:
        assert not report["checks"][failed]["ok"]
    if artifact == "deleted":
        assert report["checks"]["rows"] == {
            "ok": False, "expected": 3, "actual": 2,
            "expected_by_file": [{"file": str(source / "a.parquet"), "rows": 2},
                                 {"file": str(source / "b.parquet"), "rows": 1}],
            "actual_by_file": [{"file": str(paths[artifact]), "rows": 2}]}
    if artifact == "type":
        issue = report["checks"]["schema"]["issues"][0]
        assert issue["file"] == str(paths[artifact]) and issue["path_parts"] == ["id"]
        assert issue["expected"] == "int64" and issue["actual"] == "int32"
    if artifact == "nulls":
        assert report["checks"]["null_counts"]["columns"]["note"] == {
            "ok": False, "expected": 2, "actual": 1, "unavailable": [],
            "expected_by_file": [{"file": str(source / "a.parquet"), "nulls": 1},
                                 {"file": str(source / "b.parquet"), "nulls": 1}],
            "actual_by_file": [{"file": str(paths[artifact]), "nulls": 1}]}
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    assert report["output"]["files"] == [str(paths[artifact])]


def test_value_semantics_are_explicitly_unchecked(artifacts, capsys):
    source, paths = artifacts
    code, report = validate(source, paths["values"], capsys, "--null-column", "note")
    assert code == 0 and report["ok"]
    assert report["scope"]["values_checked"] is False


def test_schema_and_row_checks_need_no_data_decode(artifacts, capsys, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("no null columns requested; footer-only check")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    source, paths = artifacts
    code, report = validate(source, paths["good"], capsys)
    assert code == 0 and report["expected"]["null_counts"] == {}


def test_max_rows_rejected_before_decode(artifacts, capsys, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("row budget must be checked before decoding")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    source, paths = artifacts
    assert main(["validate", str(source), "--output", str(paths["good"]),
                 "--null-column", "note", "--max-rows", "2"]) == 2
    assert "max-rows" in capsys.readouterr().err


def test_literal_names_parent_nulls_and_empty_files(tmp_path, capsys):
    source = tmp_path / "input.parquet"
    schema = pa.schema([("a.b", pa.string()), ("a", pa.struct([("b", pa.int32())])),
                        ("*", pa.list_(pa.int32())), ("^x$", pa.null())])
    table = pa.Table.from_pylist([{"a.b": None, "a": {"b": None}, "*": [None], "^x$": None},
                                  {"a.b": "x", "a": None, "*": None, "^x$": None}], schema=schema)
    pq.write_table(table, source)
    output = tmp_path / "output"
    output.mkdir()
    pq.write_table(table, output / "data.parquet")
    pq.write_table(table.slice(0, 0), output / "empty.parquet")
    code, report = validate(source, output, capsys, "--null-column", "a.b", "--null-column", "a",
                            "--null-column", "*", "--null-column", "^x$", "--batch-size", "1")
    assert code == 0
    assert report["actual"]["null_counts"] == {"a.b": 1, "a": 1, "*": 1, "^x$": 2}
    assert len(report["actual"]["schema_by_file"]) == 2


def test_missing_selected_output_column_is_unknown_not_synthetic_nulls(artifacts, tmp_path, capsys):
    source, paths = artifacts
    output = tmp_path / "missing.parquet"
    pq.write_table(pq.read_table(paths["good"]).drop(["note"]), output)
    code, report = validate(source, output, capsys, "--null-column", "note")
    assert code == 1 and not report["checks"]["schema"]["ok"]
    assert report["actual"]["null_counts"]["note"] is None
    assert not report["checks"]["null_counts"]["ok"]


def test_nested_schema_change_and_column_order_are_located(tmp_path, capsys):
    source, output = tmp_path / "source.parquet", tmp_path / "output.parquet"
    pq.write_table(pa.table({"payload": pa.array([{"n": 1}], pa.struct([("n", pa.int64())])),
                             "key": [1]}), source)
    pq.write_table(pa.table({"key": [1], "payload": pa.array([{"n": 1}], pa.struct([("n", pa.int32())]))}), output)
    code, report = validate(source, output, capsys)
    assert code == 1
    issues = report["checks"]["schema"]["issues"]
    assert any(i["path_parts"] == [] and i["reason"] == "field_order" for i in issues)
    assert any(i["path_parts"] == ["payload", "n"] and i["actual"] == "int32" for i in issues)


def test_hive_missing_and_null_partitions_are_materialized(tmp_path, capsys):
    source = tmp_path / "input"
    for value in ["001", "__HIVE_DEFAULT_PARTITION__"]:
        part = source / ("k=" + value)
        part.mkdir(parents=True)
        pq.write_table(pa.table({"id": [1, 2]}), part / "p.parquet")
    output = tmp_path / "output.parquet"
    pq.write_table(pa.table({"id": [1, 2, 1, 2], "k": ["001", "001", None, None]}), output)
    code, report = validate(source, output, capsys, "--hive", "--null-column", "k")
    assert code == 0 and report["expected"]["null_counts"] == {"k": 2}
    assert report["actual"]["null_counts"] == {"k": 2}


def test_policy_stringification_and_invalid_null_column(tmp_path, capsys):
    source = tmp_path / "input"
    source.mkdir()
    pq.write_table(pa.table({"x": [1, None]}), source / "a.parquet")
    pq.write_table(pa.table({"x": ["001"]}), source / "b.parquet")
    output = tmp_path / "output.parquet"
    pq.write_table(pa.table({"x": ["1", None, "001"]}), output)
    args = ["validate", str(source), "--output", str(output), "--null-column", "x"]
    assert main(args) == 1
    assert "conflict:" in capsys.readouterr().err
    assert main(args + ["--policy", "stringify-conflicts"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert main(args + ["--policy", "stringify-conflicts", "--null-column", "missing"]) == 2
    assert "unknown top-level" in capsys.readouterr().err


def test_output_unreadable_never_skipped_and_declared_file_scope(artifacts, tmp_path, capsys):
    source, paths = artifacts
    (source / "skip.txt").write_text("bad")
    args = ["validate", str(source), "--output", str(paths["good"]), "--skip-unreadable"]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert len(json.loads(captured.out)["input"]["unreadable"]) == 1
    output = tmp_path / "bad-output"
    output.mkdir()
    pq.write_table(pq.read_table(paths["good"]), output / "good.parquet")
    (output / "unexpected.txt").write_text("bad")
    assert main(["validate", str(source), "--output", str(output), "--skip-unreadable"]) == 2
    assert "output unreadable" in capsys.readouterr().err
    alias = tmp_path / "alias.parquet"
    alias.symlink_to(paths["good"])
    assert main(["validate", str(source), "--output", str(alias), "--skip-unreadable"]) == 2
    assert "symlink" in capsys.readouterr().err


@pytest.mark.parametrize("option,value", [("--batch-size", "0"), ("--batch-size", "-1"), ("--max-rows", "-1")])
def test_invalid_resource_limits_rejected(artifacts, capsys, option, value):
    source, paths = artifacts
    assert main(["validate", str(source), "--output", str(paths["good"]), option, value]) == 2
    assert option[2:] in capsys.readouterr().err


def test_existing_rewrite_artifact_and_bounded_batches(dataset, tmp_path, capsys, monkeypatch):
    from datawrench.parquet_unify.catalog import plan, scan
    from datawrench.parquet_unify.operations import rewrite
    catalog = scan(dataset)
    output = tmp_path / "rewritten"
    rewrite(catalog, plan(catalog), output)
    original = pq.ParquetFile.iter_batches
    sizes = []

    def batches(reader, *args, **kwargs):
        assert kwargs["use_threads"] is False
        for batch in original(reader, *args, **kwargs):
            sizes.append(batch.num_rows)
            yield batch

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    code, report = validate(dataset, output, capsys, "--null-column", "note", "--null-column", "note",
                            "--null-column", "profile", "--batch-size", "1", "--max-rows", "5")
    assert code == 0
    assert report["expected"]["null_counts"] == {"note": 4, "profile": 1}
    assert report["actual"]["null_counts"] == {"note": 4, "profile": 1}
    assert sizes and max(sizes) == 1


def test_each_output_schema_checked_even_when_union_would_match(artifacts, tmp_path, capsys):
    source, paths = artifacts
    output = tmp_path / "mixed"
    output.mkdir()
    table = pq.read_table(paths["good"])
    pq.write_table(table.slice(0, 2), output / "a.parquet")
    pq.write_table(table.slice(2).drop(["note"]), output / "b.parquet")
    code, report = validate(source, output, capsys, "--null-column", "note")
    assert code == 1 and report["checks"]["rows"]["ok"]
    assert report["checks"]["schema"]["issues"] == [
        {"file": str(output / "b.parquet"), "path": "/note", "path_parts": ["note"],
         "reason": "missing_field", "expected": "string", "actual": None}]
    assert report["actual"]["null_counts"] == {"note": None}


def test_output_row_budget_also_checked(artifacts, tmp_path, capsys):
    source, paths = artifacts
    output = tmp_path / "too-many.parquet"
    pq.write_table(pa.concat_tables([pq.read_table(paths["good"])] * 2), output)
    assert main(["validate", str(source), "--output", str(output), "--max-rows", "3"]) == 2
    assert "output rows 6 exceed max-rows 3" in capsys.readouterr().err


def test_all_empty_artifact_and_metadata_carriers(tmp_path, capsys):
    source = tmp_path / "input.parquet"
    output = tmp_path / "output.parquet"
    expected = pa.schema([("text", pa.string()), ("items", pa.list_(pa.int64()))])
    observed = pa.schema([pa.field("text", pa.large_string(), nullable=False),
                          pa.field("items", pa.large_list(pa.field("renamed", pa.int64(), nullable=False)))],
                         metadata={b"writer": b"synthetic"})
    pq.write_table(pa.Table.from_batches([], schema=expected), source)
    pq.write_table(pa.Table.from_batches([], schema=observed), output)
    code, report = validate(source, output, capsys, "--null-column", "text", "--max-rows", "0")
    assert code == 0 and report["actual"]["rows"] == 0
    assert report["actual"]["null_counts"] == {"text": 0}


@pytest.mark.parametrize("changed", [pa.timestamp("ms", "UTC"), pa.timestamp("us", "Europe/Berlin")])
def test_artifact_timestamp_unit_and_zone_must_match_plan(tmp_path, capsys, changed):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    pq.write_table(pa.table({"t": pa.array([0], pa.timestamp("us", "UTC"))}), source)
    pq.write_table(pa.table({"t": pa.array([0], changed)}), output)
    code, report = validate(source, output, capsys)
    assert code == 1
    assert report["checks"]["schema"]["issues"][0]["path_parts"] == ["t"]


def test_duplicate_nested_artifact_fields_reported_without_crash(tmp_path, capsys):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    pq.write_table(pa.table({"p": pa.array([{"x": 1}], pa.struct([("x", pa.int64())]))}), source)
    duplicate = pa.StructArray.from_arrays([pa.array([1]), pa.array([2])], names=["x", "x"])
    pq.write_table(pa.table({"p": duplicate}), output)
    code, report = validate(source, output, capsys, "--null-column", "p")
    assert code == 1
    assert any(i["reason"] == "duplicate_fields" and i["path_parts"] == ["p"]
               for i in report["checks"]["schema"]["issues"])


@pytest.mark.parametrize("artifact,selected,exit_code", [("good", ["id", "note"], 0),
                                                        ("values", ["id", "note"], 1),
                                                        ("values", ["note"], 0),
                                                        ("nulls", ["note"], 1)])
def test_checksum_selected_value_contract(artifacts, capsys, artifact, selected, exit_code):
    source, paths = artifacts
    files = list(source.iterdir()) + list(paths.values())
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    options = [arg for name in selected + selected for arg in ("--checksum-column", name)]
    code, report = validate(source, paths[artifact], capsys, *options, "--null-column", "note", "--batch-size", "1")
    assert code == exit_code
    checks = report["checks"]["checksums"]
    assert checks["ok"] == (exit_code == 0)
    assert list(checks["columns"]) == selected
    assert report["scope"]["values_checked"] is True
    assert report["scope"]["checksum_columns"] == selected
    for name in selected:
        expected, actual = report["expected"]["checksums"][name], report["actual"]["checksums"][name]
        assert len(expected) == len(actual) == 64
        check = checks["columns"][name]
        assert {k: v for k, v in check.items() if k not in ("expected_by_file", "actual_by_file")} == {
            "ok": expected == actual, "expected": expected, "actual": actual, "unavailable": []}
        assert [r["file"] for r in check["expected_by_file"]] == sorted(str(p) for p in source.iterdir())
        assert check["actual_by_file"] == [{"file": str(paths[artifact]), "checksum": actual}]
    assert report["checks"]["rows"]["ok"] and report["checks"]["schema"]["ok"]
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}


def test_checksum_batches_use_planned_values_without_engines(dataset, tmp_path, capsys, monkeypatch):
    from datawrench.parquet_unify.catalog import plan, scan
    from datawrench.parquet_unify.data import source_summary
    from datawrench.parquet_unify.operations import rewrite
    import datawrench.parquet_unify.cli as cli
    catalog = scan(dataset)
    target = plan(catalog)
    expected = source_summary(catalog, target)["checksums"]
    output = tmp_path / "rewritten"
    rewrite(catalog, target, output)
    original = pq.ParquetFile.iter_batches
    sizes, files = [], []

    def batches(reader, *args, **kwargs):
        assert kwargs["use_threads"] is False
        files.append(reader)
        for batch in original(reader, *args, **kwargs):
            sizes.append(batch.num_rows)
            yield batch

    def fail(*args, **kwargs):
        raise AssertionError("no engine execution, row-group reads or final table materialization")

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", fail)
    monkeypatch.setattr(pq.ParquetFile, "read", fail)
    monkeypatch.setattr(cli, "verify", fail)
    monkeypatch.setattr(cli, "rewrite", fail)
    options = [arg for name in target.names for arg in ("--checksum-column", name)]
    code, report = validate(dataset, output, capsys, *options, "--null-column", "note", "--batch-size", "1")
    assert code == 0 and report["expected"]["checksums"] == expected == report["actual"]["checksums"]
    assert report["actual"]["null_counts"] == {"note": 4}
    assert max(sizes) == 1
    assert len(files) == len(catalog.entries) + len(list(output.glob("*.parquet")))  # One pass per file.


@pytest.mark.parametrize("name", ["ID", "missing", "id.child"])
def test_checksum_unknown_names_rejected_before_decode(artifacts, capsys, monkeypatch, name):
    def fail(*args, **kwargs):
        raise AssertionError("invalid selection must not decode")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    source, paths = artifacts
    assert main(["validate", str(source), "--output", str(paths["good"]), "--checksum-column", name]) == 2
    assert "unknown top-level checksum columns" in capsys.readouterr().err


@pytest.mark.parametrize("side", ["input", "output"])
def test_checksum_row_budget_precedes_decode(artifacts, tmp_path, capsys, monkeypatch, side):
    source, paths = artifacts
    output = paths["good"]
    if side == "output":
        output = tmp_path / "large.parquet"
        pq.write_table(pa.concat_tables([pq.read_table(paths["good"])] * 2), output)
    def fail(*args, **kwargs):
        raise AssertionError("budget must precede decoding")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", fail)
    limit = "2" if side == "input" else "3"
    assert main(["validate", str(source), "--output", str(output), "--checksum-column", "id",
                 "--max-rows", limit]) == 2
    assert side + " rows" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["missing", "type", "nested"])
def test_checksum_incompatible_artifact_is_unknown(tmp_path, capsys, kind):
    source, output = tmp_path / "input.parquet", tmp_path / "output"
    output.mkdir()
    target = pa.table({"x": pa.array([None], pa.struct([("n", pa.int64())])), "id": [1]})
    pq.write_table(target, source)
    pq.write_table(target, output / "a.parquet")
    if kind == "missing":
        bad = target.drop(["x"])
    elif kind == "type":
        bad = target.set_column(0, "x", pa.array(["incompatible"]))
    else:
        duplicate = pa.StructArray.from_arrays([pa.array([1]), pa.array([2])], names=["n", "n"])
        bad = target.set_column(0, "x", duplicate)
    pq.write_table(bad, output / "b.parquet")
    code, report = validate(source, output, capsys, "--checksum-column", "x", "--checksum-column", "id")
    assert code == 1 and not report["checks"]["schema"]["ok"]
    assert report["actual"]["checksums"]["x"] is None
    assert not report["checks"]["checksums"]["columns"]["x"]["ok"]
    assert report["actual"]["checksums"]["id"] is not None


def test_checksum_hive_missing_null_and_stringification(tmp_path, capsys):
    source = tmp_path / "input"
    for directory, table in [("k=001", pa.table({"x": [1, None]})),
                             ("k=__HIVE_DEFAULT_PARTITION__", pa.table({"x": ["001"]})),
                             ("plain", pa.table({"id": [1]}))]:
        part = source / directory
        part.mkdir(parents=True)
        pq.write_table(table, part / "p.parquet")
    output = tmp_path / "output.parquet"
    pq.write_table(pa.table({"x": ["1", None, "001", None], "k": ["001", "001", None, None],
                             "id": pa.array([None, None, None, 1], pa.int64())}), output)
    code, report = validate(source, output, capsys, "--hive", "--policy", "stringify-conflicts",
                            "--checksum-column", "x", "--checksum-column", "k", "--checksum-column", "id")
    assert code == 0 and report["expected"]["checksums"] == report["actual"]["checksums"]


def test_checksum_literal_names_nested_nulls_and_ns(tmp_path, capsys):
    from decimal import Decimal
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    table = pa.table({"a.b": ["literal", None, "dup", "dup"],
                      "a": pa.array([{"b": 1}, None, {"b": None}, {"b": 2}], pa.struct([("b", pa.int32())])),
                      "*": pa.array([[None], [], None, [1]], pa.list_(pa.int32())),
                      "^x$": pa.nulls(4), "x/y~": [b"\x00", None, b"a", b"a"],
                      "n": pa.array([1, 2, None, 3], pa.timestamp("ns", "UTC")),
                      "d": pa.array([Decimal("1.20"), None, Decimal("0.00"), Decimal("1.20")], pa.decimal128(6, 2)),
                      "f": [float("nan"), -0.0, 1.0, None]})
    pq.write_table(table, source, row_group_size=3)
    reordered = table.take(pa.array([3, 1, 2, 0]))
    reordered = reordered.set_column(7, "f", pa.array([None, 0.0, 1.0, float("nan")]))
    pq.write_table(reordered, output, row_group_size=2)
    options = [arg for name in table.column_names for arg in ("--checksum-column", name)]
    code, report = validate(source, output, capsys, *options, "--batch-size", "1")
    assert code == 0 and report["checks"]["checksums"]["ok"]
    changed = reordered.set_column(5, "n", pa.array([3, 2, None, 2], pa.timestamp("ns", "UTC")))
    pq.write_table(changed, output)
    code, report = validate(source, output, capsys, *options)
    assert code == 1 and not report["checks"]["checksums"]["columns"]["n"]["ok"]
    changed = reordered.set_column(0, "a.b", pa.array(["literal", None, "literal", "dup"]))
    pq.write_table(changed, output)
    code, report = validate(source, output, capsys, *options)
    assert code == 1 and not report["checks"]["checksums"]["columns"]["a.b"]["ok"]


def test_checksum_row_association_remains_unchecked(tmp_path, capsys):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    pq.write_table(pa.table({"id": [1, 2], "note": ["a", "b"]}), source)
    pq.write_table(pa.table({"id": [1, 2], "note": ["b", "a"]}), output)
    code, report = validate(source, output, capsys, "--checksum-column", "id", "--checksum-column", "note")
    assert code == 0 and report["checks"]["checksums"]["ok"]


def test_checksum_empty_artifacts_and_offset_carriers(tmp_path, capsys):
    source, output = tmp_path / "input.parquet", tmp_path / "output.parquet"
    schema = pa.schema([("x", pa.string()), ("items", pa.list_(pa.int64()))])
    carriers = pa.schema([("x", pa.large_string()), ("items", pa.large_list(pa.int64()))])
    for data in [[], [{"x": "a", "items": [1, None]}, {"x": None, "items": []}]]:
        pq.write_table(pa.Table.from_pylist(data, schema=schema), source)
        pq.write_table(pa.Table.from_pylist(data, schema=carriers), output)
        code, report = validate(source, output, capsys, "--checksum-column", "x", "--checksum-column", "items")
        assert code == 0 and report["expected"]["checksums"] == report["actual"]["checksums"]


def test_checksum_full_date32_domain(tmp_path, capsys, monkeypatch):
    # Date32 covers all int32 epoch days, beyond Python datetime's years 1..9999.
    # Canonical payloads: out-of-range {"date32_days":N}, in-range ISO strings,
    # null, and a duplicate year-10000 date. Golden digest is independent of Summary.
    days = [-2147483648, -719163, -719162, -1, 0, 2932896, 2932897, 2147483647, None, 2932897]
    table = pa.table({"dates": pa.array(days, pa.date32()), "note": ["kept"] * len(days)})
    source = tmp_path / "dates-input.parquet"
    good, changed = tmp_path / "dates-good.parquet", tmp_path / "dates-changed.parquet"
    pq.write_table(table, source, row_group_size=3)
    reordered = table.take(pa.array(list(reversed(range(len(days))))))
    pq.write_table(reordered, good, row_group_size=4)
    changed_days = list(reversed(days))
    changed_days[0] += 1
    pq.write_table(reordered.set_column(0, "dates", pa.array(changed_days, pa.date32())), changed)
    paths = [source, good, changed]
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths}
    original = pq.ParquetFile.iter_batches
    sizes = []

    def batches(reader, *args, **kwargs):
        assert kwargs["use_threads"] is False
        for batch in original(reader, *args, **kwargs):
            sizes.append(batch.num_rows)
            yield batch

    def fail(*args, **kwargs):
        raise AssertionError("no whole-file or row-group materialization")

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", batches)
    monkeypatch.setattr(pq.ParquetFile, "read", fail)
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", fail)
    options = ("--checksum-column", "dates", "--checksum-column", "note", "--null-column", "dates",
               "--batch-size", "1", "--max-rows", str(len(days)))
    code, report = validate(source, good, capsys, *options)
    assert code == 0 and report["ok"]
    assert report["expected"]["checksums"]["dates"] == "11ede1ebc48999165fd7bd60605982d040c77ee76719255da187f9dfaacc765d"
    assert report["expected"]["checksums"] == report["actual"]["checksums"]
    assert report["expected"]["null_counts"] == report["actual"]["null_counts"] == {"dates": 1}
    code, report = validate(source, changed, capsys, *options)
    assert code == 1 and not report["checks"]["checksums"]["columns"]["dates"]["ok"]
    assert report["checks"]["checksums"]["columns"]["note"]["ok"]
    assert all(report["checks"][key]["ok"] for key in ("schema", "rows", "null_counts"))
    assert sizes and max(sizes) == 1
    assert validate(source, changed, capsys, "--checksum-column", "note")[0] == 0
    assert validate(source, changed, capsys, "--null-column", "dates")[0] == 0
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths}


def test_checksum_nested_date32_domain_with_missing_fields(tmp_path, capsys):
    source = tmp_path / "nested-dates-input"
    source.mkdir()
    pq.write_table(pa.table({
        "p": pa.array([{"d": 2932897}, None, {"d": None}], pa.struct([("d", pa.date32())])),
        "items": pa.array([[-2147483648, None], [], None], pa.list_(pa.date32())),
    }), source / "a.parquet", row_group_size=2)
    pq.write_table(pa.table({"p": pa.array([{"label": "missing"}], pa.struct([("label", pa.string())]))}),
                   source / "b.parquet")
    schema = pa.schema([("p", pa.struct([("d", pa.date32()), ("label", pa.string())])),
                        ("items", pa.list_(pa.date32()))])
    rows = [{"p": {"d": 2932897, "label": None}, "items": [-2147483648, None]},
            {"p": None, "items": []}, {"p": {"d": None, "label": None}, "items": None},
            {"p": {"d": None, "label": "missing"}, "items": None}]
    good, changed = tmp_path / "nested-good.parquet", tmp_path / "nested-changed.parquet"
    pq.write_table(pa.Table.from_pylist(list(reversed(rows)), schema=schema), good)
    rows[0]["p"]["d"] += 1
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), changed)
    options = ("--checksum-column", "p", "--checksum-column", "items", "--null-column", "p", "--batch-size", "1")
    code, report = validate(source, good, capsys, *options)
    assert code == 0 and report["expected"]["checksums"] == report["actual"]["checksums"]
    assert report["expected"]["null_counts"] == report["actual"]["null_counts"] == {"p": 1}
    code, report = validate(source, changed, capsys, *options)
    assert code == 1 and not report["checks"]["checksums"]["columns"]["p"]["ok"]
    assert report["checks"]["checksums"]["columns"]["items"]["ok"]
    assert all(report["checks"][key]["ok"] for key in ("schema", "rows", "null_counts"))
