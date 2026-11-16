import json
import os
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench import __version__
from datawrench.parquet_unify.catalog import plan, scan
from datawrench.parquet_unify.cli import main
from datawrench.parquet_unify.operations import rewrite, verify
from datawrench.parquet_unify.recipes import duckdb_recipe
from datawrench.parquet_unify.types import Conflict, UnifyError, flatten


def test_cli_formats_and_exit_codes(dataset, capsys):
    assert main(["scan", str(dataset), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["files"] == 2 and report["rows"] == 5
    assert main(["scan", str(dataset), "--exit-code", "--format", "md"]) == 1
    assert "| Classification |" in capsys.readouterr().out
    assert main(["plan", str(dataset), "--policy", "strict"]) == 1
    error = capsys.readouterr().err
    assert "strict" in error and "part-0.parquet" in error and "/profile/city" in error
    assert main(["plan", str(dataset), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"][0] == {"name": "id", "type": "int64"}
    assert main(["recipe", str(dataset), "--engine", "duckdb"]) == 0
    assert "UNION ALL BY NAME" in capsys.readouterr().out
    assert main(["verify", str(dataset)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]


def test_empty_missing_unreadable_and_skip(tmp_path, capsys):
    assert main(["scan", str(tmp_path)]) == 2
    assert "empty" in capsys.readouterr().err
    assert main(["scan", str(tmp_path / "missing")]) == 2
    capsys.readouterr()
    bad = tmp_path / "corrupt.parquet"
    bad.write_bytes(b"not parquet")
    assert main(["scan", str(tmp_path), "--format", "json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["files"] == 0 and report["unreadable"][0]["file"] == str(bad)
    assert main(["scan", str(tmp_path), "--skip-unreadable"]) == 2
    assert "no readable" in capsys.readouterr().err
    pq.write_table(pa.table({"x": [1]}), tmp_path / "valid.parquet")
    (tmp_path / "notes.txt").write_text("non-Parquet files must be listed too")
    assert main(["scan", str(tmp_path), "--format", "json"]) == 2
    assert len(json.loads(capsys.readouterr().out)["unreadable"]) == 2
    assert main(["plan", str(tmp_path)]) == 2
    assert "unreadable" in capsys.readouterr().err
    out = tmp_path.parent / (tmp_path.name + "-output")
    assert main(["rewrite", str(tmp_path), "--out", str(out)]) == 2
    capsys.readouterr()
    assert not out.exists()
    assert main(["verify", str(tmp_path), "--skip-unreadable"]) == 0
    captured = capsys.readouterr()
    assert len(json.loads(captured.out)["unreadable"]) == 2
    assert "excluding" in captured.err


def test_cli_installed_entrypoint_and_error_code(tmp_path):
    command = [sys.executable, "-m", "datawrench", "parquet-unify"]
    process = subprocess.run([*command, "scan", str(tmp_path)], capture_output=True, text=True)
    assert process.returncode == 2 and "empty input" in process.stderr
    version = subprocess.run([*command, "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == __version__


def test_scan_footer_only(dataset, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("scan must never read column data")
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", fail)
    monkeypatch.setattr(pq.ParquetFile, "read", fail)
    monkeypatch.setattr(pq, "read_table", fail)
    report = scan(dataset).report()
    assert report["files"] == 2
    assert report["rows"] == 5


def test_input_symlink_and_special_file_boundary(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    pq.write_table(pa.table({"x": [1]}), outside / "ok.parquet")
    root = tmp_path / "input"
    root.mkdir()
    (root / "link.parquet").symlink_to(outside / "ok.parquet")
    (root / "linked_dir").symlink_to(outside, target_is_directory=True)
    os.mkfifo(root / "pipe")
    cat = scan(root)
    assert not cat.entries
    assert len(cat.unreadable) == 3
    with pytest.raises(UnifyError, match="symlink"):
        scan(root / "link.parquet")


def test_sql_python_literal_and_identifier_escaping(tmp_path):
    root = tmp_path / "quote' folder; --"
    root.mkdir()
    # These strings would be dangerous if inserted as SQL/Python code instead of literals.
    name = "x\"; DROP TABLE secrets; --"
    for i in range(2):
        pq.write_table(pa.table({name: pa.array([i], type=pa.int32() if i == 0 else pa.int64())}),
                       root / ("p'\n" + str(i) + ".parquet"))
    cat = scan(root)
    result = verify(cat, plan(cat))
    assert result["ok"], result
    assert result["reference"]["rows"] == 2


def test_case_collision_and_glob_path_rejected_explicitly(tmp_path):
    pq.write_table(pa.table({"A": [1], "a": [2]}), tmp_path / "case.parquet")
    cat = scan(tmp_path)
    with pytest.raises(UnifyError, match="case-insensitively"):
        duckdb_recipe(cat, plan(cat))
    special = tmp_path / "[glob].parquet"
    pq.write_table(pa.table({"x": [1]}), special)
    single = scan(special)
    with pytest.raises(UnifyError, match="glob metacharacters"):
        duckdb_recipe(single, plan(single))


def test_metadata_is_ignored_and_duplicate_fields_are_rejected(tmp_path):
    for i in range(2):
        schema = pa.schema([pa.field("x", pa.int32(), nullable=bool(i), metadata={b"writer": str(i).encode()})])
        pq.write_table(pa.Table.from_arrays([pa.array([1], pa.int32())], schema=schema), tmp_path / f"{i}.parquet")
    cat = scan(tmp_path)
    assert len(cat.groups()) == 1
    assert plan(cat, "strict").names == ["x"]
    pq.write_table(pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["x", "x"]), tmp_path / "duplicate.parquet")
    cat = scan(tmp_path)
    assert "duplicate column" in cat.unreadable[0]["error"]


def test_rewrite_symlink_destination_and_new_parent_rejected(dataset, tmp_path):
    cat = scan(dataset)
    target = plan(cat)
    (tmp_path / "alias").symlink_to(tmp_path / "absent", target_is_directory=True)
    with pytest.raises(UnifyError, match="symlink"):
        rewrite(cat, target, tmp_path / "alias")
    with pytest.raises(UnifyError, match="parent"):
        rewrite(cat, target, tmp_path / "new-parent" / "out")
    assert not (tmp_path / "absent").exists()


def test_unsupported_map_is_explicit(tmp_path, capsys):
    pq.write_table(pa.table({"m": pa.array([[('key', 1)]], pa.map_(pa.string(), pa.int64()))}), tmp_path / "map.parquet")
    assert main(["scan", str(tmp_path), "--exit-code"]) == 1
    assert "unsupported Arrow type" in capsys.readouterr().out
    assert main(["plan", str(tmp_path)]) == 1
    assert "unsupported Arrow type" in capsys.readouterr().err


def test_nested_pointer_paths_escape_field_names():
    schema = pa.schema([("a/b~c", pa.struct([("x/y", pa.int32())]))])
    assert "/a~1b~0c/x~1y" in flatten(schema)


def test_deep_list_paths():
    schema = pa.schema([("matrix", pa.list_(pa.list_(pa.struct([("x", pa.int32())]))))])
    assert "/matrix/[]/[]/x" in flatten(schema)


def test_nested_conflict_diagnostic_names_field(tmp_path):
    for i, t in enumerate((pa.int32(), pa.string())):
        value = 1 if i == 0 else "x"
        pq.write_table(pa.table({"outer": pa.array([{"inner": value}], pa.struct([("inner", t)]))}), tmp_path / f"{i}.parquet")
    with pytest.raises(Conflict, match="struct field 'inner'"):
        plan(scan(tmp_path))
