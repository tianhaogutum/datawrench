from decimal import Decimal
import json
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.catalog import plan, scan
from datawrench.parquet_unify.data import Summary, normalize_array, source_summary
from datawrench.parquet_unify.operations import _arrow_table, rewrite, verify
from datawrench.parquet_unify.recipes import duckdb_recipe, polars_recipe
from datawrench.parquet_unify.types import Conflict, UnifyError


def write(path, **columns):
    pq.write_table(pa.table(columns), path, row_group_size=2)


def test_real_end_to_end_and_naive_errors(dataset):
    catalog = scan(dataset)
    report = catalog.report()
    assert report["rows"] == 5
    assert len(report["groups"]) == 2
    paths = {c["path"]: c for c in report["columns"]}
    assert paths["/profile/city"]["classification"] == "missing_in_some"
    assert paths["/tags/[]"]["classification"] == "widenable"
    target = plan(catalog)
    result = verify(catalog, target)
    assert result["ok"], result
    for engine in ("duckdb", "polars"):
        assert result["engines"][engine]["rows"] == 5
        assert result["engines"][engine]["checksums"] == result["reference"]["checksums"]
        assert result["naive"][engine]["status"] == "error"


def test_recipes_standalone_without_package(dataset, tmp_path):
    catalog = scan(dataset)
    target = plan(catalog)
    sql = duckdb_recipe(catalog, target)
    import duckdb
    with duckdb.connect(config={"threads": 2}) as db:
        assert _arrow_table(db.execute(sql)).num_rows == 5
    code = polars_recipe(catalog, target)
    script = tmp_path / "standalone.py"
    script.write_text(code + "\nassert result.collect(engine='streaming').height == 5\n")
    process = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert "parquet_unify" not in code and "import datawrench" not in code


def test_rewrite_single_fingerprint_and_checksum(dataset, tmp_path):
    catalog = scan(dataset)
    target = plan(catalog)
    before = source_summary(catalog, target)
    result = rewrite(catalog, target, tmp_path / "unified")
    assert result["checksums"] == before["checksums"]
    rewritten = scan(tmp_path / "unified")
    assert len(rewritten.groups()) == 1
    assert source_summary(rewritten, plan(rewritten)) == before
    assert verify(rewritten, plan(rewritten))["ok"]
    assert len(scan(dataset).groups()) == 2  # sources unchanged
    with pytest.raises(UnifyError, match="must not exist"):
        rewrite(catalog, target, dataset)
    with pytest.raises(UnifyError, match="outside"):
        rewrite(catalog, target, dataset / "nested")
    with pytest.raises(UnifyError, match="must not exist"):
        rewrite(catalog, target, tmp_path / "unified")


def test_rewrite_failure_cleans_staging(dataset, tmp_path, monkeypatch):
    import datawrench.parquet_unify.operations as ops
    def fail(*args):
        raise UnifyError("synthetic read failure")
    monkeypatch.setattr(ops, "entry_batches", fail)
    with pytest.raises(UnifyError, match="synthetic"):
        rewrite(scan(dataset), plan(scan(dataset)), tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".parquet-unify-*"))


def test_hive_explicit_strings_and_nulls(tmp_path, make_dataset):
    path = tmp_path / "hive"
    make_dataset(path, "hive")
    catalog = scan(path, hive=True)
    assert plan(catalog).field("day").type == pa.string()
    result = verify(catalog, plan(catalog))
    assert result["ok"], result
    flat = tmp_path / "flat"
    rewrite(catalog, plan(catalog), flat)
    assert source_summary(scan(flat), plan(catalog)) == result["reference"]
    # Explicit partition literals preserve leading zeros, URL encoding and null marker.
    root = tmp_path / "encoded"
    for key in ("001", "a%2Fb", "__HIVE_DEFAULT_PARTITION__"):
        part = root / ("k=" + key)
        part.mkdir(parents=True)
        write(part / "p.parquet", value=pa.array([1]))
    cat = scan(root, hive=True)
    assert {e.partitions["k"] for e in cat.entries} == {"001", "a/b", None}
    assert verify(cat, plan(cat))["ok"]


def test_hive_collision_is_unreadable(tmp_path):
    directory = tmp_path / "k=v"
    directory.mkdir()
    write(directory / "a.parquet", k=pa.array(["v"]))
    cat = scan(tmp_path, hive=True)
    assert len(cat.unreadable) == 1
    assert "collision" in cat.unreadable[0]["error"]


def test_stringify_is_explicit(tmp_path, make_dataset):
    root = tmp_path / "conflict"
    make_dataset(root, "conflict")
    cat = scan(root)
    assert cat.report()["columns"][0]["classification"] == "conflict"
    with pytest.raises(Conflict, match="value"):
        plan(cat)
    target = plan(cat, "stringify-conflicts")
    result = verify(cat, target)
    assert result["ok"], result
    rewrite(cat, target, tmp_path / "strings")
    assert pq.read_table(tmp_path / "strings").column("value").to_pylist() == ["1", None, "001", "x"]


@pytest.mark.parametrize("scenario", ["float-decimal", "timezone"])
def test_real_conflict_fixtures(tmp_path, make_dataset, scenario):
    make_dataset(tmp_path / scenario, scenario)
    with pytest.raises(Conflict):
        plan(scan(tmp_path / scenario))


def test_nested_lists_struct_null_masks(tmp_path):
    t1 = pa.list_(pa.struct([("a", pa.int32()), ("nested", pa.list_(pa.int32()))]))
    t2 = pa.list_(pa.struct([("a", pa.int64()), ("b", pa.string()), ("nested", pa.list_(pa.int64()))]))
    write(tmp_path / "a.parquet", col=pa.array([[{"a": 1, "nested": [1, None]}, None], None, []], t1))
    write(tmp_path / "b.parquet", col=pa.array([[{"a": 2**40, "b": "z", "nested": [2**40]}]], t2))
    cat = scan(tmp_path)
    result = verify(cat, plan(cat))
    assert result["ok"], result
    array = pa.array([[1], None, [], [2, None]], pa.list_(pa.int32())).slice(1, 3)
    assert normalize_array(array, pa.list_(pa.int64())).to_pylist() == [None, [], [2, None]]


@pytest.mark.parametrize("types,values", [
    ([pa.int64(), pa.uint64()], [[-2**63], [2**64-1]]),
    ([pa.int64(), pa.decimal128(22, 3)], [[2**63-1], [Decimal("1.234")]]),
    ([pa.decimal128(9, 1), pa.decimal128(9, 5)], [[Decimal("12345678.9")], [Decimal("1234.56789")]]),
    ([pa.date32(), pa.timestamp("ns")], [[0], [1_700_000_000_000_000_123]]),
    ([pa.timestamp("ms"), pa.timestamp("ns")], [[1_700_000_000_001], [1_700_000_000_000_000_123]]),
    ([pa.timestamp("ms", "Europe/Berlin"), pa.timestamp("us", "UTC")], [[1], [1_000_001]]),
    ([pa.string(), pa.large_string()], [["😀"], ["a"]]),
    ([pa.binary(), pa.large_binary()], [[b"\x00\xff"], [b"x"]]),
    ([pa.null(), pa.int32()], [[None], [1]]),
    ([pa.null(), pa.null()], [[None], [None]]),
    ([pa.float32(), pa.float64()], [[-0.0, float("inf")], [float("nan"), -float("inf")]]),
])
def test_engine_mapping_boundaries(tmp_path, types, values):
    for i, (t, v) in enumerate(zip(types, values)):
        write(tmp_path / f"{i}.parquet", x=pa.array(v, t))
    cat = scan(tmp_path)
    result = verify(cat, plan(cat))
    assert result["ok"], result


def test_timezone_nanosecond_engine_error_is_reported(tmp_path):
    write(tmp_path / "a.parquet", x=pa.array([123456789], pa.timestamp("ns", "UTC")))
    cat = scan(tmp_path)
    result = verify(cat, plan(cat))
    assert not result["ok"]
    assert "nanosecond" in result["engines"]["duckdb"]["error"]
    assert result["engines"]["polars"]["ok"]


def test_engine_failure_report_contains_engine_error(dataset, monkeypatch):
    import datawrench.parquet_unify.operations as ops
    original = ops.duckdb_recipe
    monkeypatch.setattr(ops, "duckdb_recipe", lambda *args: "SELECT CAST('invalid' AS BIGINT)")
    result = verify(scan(dataset), plan(scan(dataset)))
    assert not result["ok"]
    assert "invalid" in result["engines"]["duckdb"]["error"]
    assert result["engines"]["polars"]["ok"]


def test_naive_silent_column_drop_detected(tmp_path):
    write(tmp_path / "a.parquet", id=pa.array([1]))
    write(tmp_path / "b.parquet", id=pa.array([2]), extra=pa.array(["lost"]))
    cat = scan(tmp_path)
    result = verify(cat, plan(cat))
    assert result["ok"]
    assert result["naive"]["duckdb"]["status"] == "changed"
    assert result["naive"]["polars"]["status"] in ("changed", "error")


def test_checksum_order_independent_duplicate_sensitive():
    target = pa.schema([("x", pa.int64())])
    def summary(values):
        result = Summary(target)
        result.add(pa.table({"x": pa.array(values, pa.int64())}))
        return result.report()
    assert summary([1, None, 2, 1]) == summary([2, 1, 1, None])
    assert summary([1, 1, 2, 2]) != summary([1, 2, 2, 2])


def test_committed_parquet_fixture():
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "drift"
    cat = scan(fixture)
    assert len(cat.entries) == 2
    result = verify(cat, plan(cat))
    assert result["ok"], result
    assert result["reference"]["rows"] == 5


def test_same_schema_group_multiple_files_and_empty_file(tmp_path):
    for name, values in (("a", [1, 2]), ("b", [3]), ("empty", [])):
        write(tmp_path / f"{name}.parquet", x=pa.array(values, pa.int32()))
    cat = scan(tmp_path)
    assert len(cat.groups()) == 1
    assert duckdb_recipe(cat, plan(cat)).count("FROM read_parquet") == 1
    result = verify(cat, plan(cat, "strict"))
    assert result["ok"], result
    assert result["reference"]["rows"] == 3
    assert all(x["status"] == "equal" for x in result["naive"].values())


def test_entirely_empty_file_keeps_schema_and_count(tmp_path):
    write(tmp_path / "empty.parquet", x=pa.array([], pa.int64()))
    cat = scan(tmp_path)
    result = verify(cat, plan(cat))
    assert result["ok"], result
    assert result["reference"]["rows"] == 0


def test_literal_only_null_group_preserves_multiple_rows(tmp_path):
    # Also tests missing columns and partition literals combined with an all-null source.
    first = tmp_path / "k=one"
    second = tmp_path / "k=two"
    first.mkdir()
    second.mkdir()
    write(first / "a.parquet", x=pa.nulls(3))
    write(second / "b.parquet", x=pa.array([1], pa.int32()), y=pa.array(["a"]))
    cat = scan(tmp_path, hive=True)
    result = verify(cat, plan(cat))
    assert result["ok"], result
    assert result["reference"]["rows"] == 4


@pytest.mark.parametrize('name', ['^x$', '*', 'a.b'], ids=['regex', 'wildcard', 'punctuation'])
def test_polars_literal_column_names(tmp_path, name):
    expected = [{'key': 801, name: 'reserved-801', 'x': 'literal-801'},
                {'key': 802, name: 'reserved-802', 'x': 'literal-802'}]
    write(tmp_path / 'names.parquet', key=pa.array([801, 802]),
          **{name: pa.array(['reserved-801', 'reserved-802']),
             'x': pa.array(['literal-801', 'literal-802'])})
    catalog = scan(tmp_path)
    target = plan(catalog)
    result = verify(catalog, target)
    assert result['ok'], result
    namespace = {'__name__': 'independent_recipe'}
    exec(polars_recipe(catalog, target), namespace)
    assert namespace['result'].collect(engine='streaming').sort('key').to_dicts() == expected


@pytest.mark.parametrize('name', ['^x$', '*'], ids=['regex', 'wildcard'])
def test_polars_literal_nested_field_names(tmp_path, name):
    first = pa.struct([(name, pa.int32()), ('x', pa.string())])
    second = pa.struct([(name, pa.int64()), ('x', pa.string()), ('added', pa.bool_())])
    write(tmp_path / 'first.parquet', key=pa.array([1, 2]),
          payload=pa.array([{name: 7, 'x': 'first'}, None], first))
    write(tmp_path / 'second.parquet', key=pa.array([3]),
          payload=pa.array([{name: 2**40, 'x': 'second', 'added': True}], second))
    catalog = scan(tmp_path)
    target = plan(catalog)
    result = verify(catalog, target)
    assert result['ok'], result
    namespace = {'__name__': 'independent_recipe'}
    exec(polars_recipe(catalog, target), namespace)
    assert namespace['result'].collect(engine='streaming').sort('key').to_dicts() == [
        {'key': 1, 'payload': {name: 7, 'x': 'first', 'added': None}},
        {'key': 2, 'payload': None},
        {'key': 3, 'payload': {name: 2**40, 'x': 'second', 'added': True}},
    ]
