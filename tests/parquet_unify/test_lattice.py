import itertools
import pyarrow as pa
import pytest
from datawrench.parquet_unify.types import Conflict, UnifyError, canonical, merge_many


@pytest.mark.parametrize("sources,target", [
    ([pa.int8(), pa.int16()], pa.int16()),
    ([pa.int32(), pa.int64()], pa.int64()),
    ([pa.uint8(), pa.int8()], pa.int16()),
    ([pa.uint32(), pa.int32()], pa.int64()),
    ([pa.uint64(), pa.int64()], pa.decimal128(20, 0)),
    ([pa.uint8(), pa.uint32()], pa.uint32()),
    ([pa.float32(), pa.float64()], pa.float64()),
    ([pa.float32(), pa.int32()], pa.float64()),
    ([pa.uint32(), pa.float64()], pa.float64()),
    ([pa.int32(), pa.decimal128(12, 2)], pa.decimal128(12, 2)),
    ([pa.int64(), pa.decimal128(10, 3)], pa.decimal128(22, 3)),
    ([pa.decimal128(6, 2), pa.decimal128(8, 4)], pa.decimal128(8, 4)),
    ([pa.decimal128(9, 1), pa.decimal128(9, 5)], pa.decimal128(13, 5)),
    ([pa.date32(), pa.timestamp("ms")], pa.timestamp("ms")),
    ([pa.date64(), pa.timestamp("ns")], pa.timestamp("ns")),
    ([pa.timestamp("ms"), pa.timestamp("ns")], pa.timestamp("ns")),
    ([pa.timestamp("s")], pa.timestamp("ms")),
    ([pa.timestamp("ms", "Europe/Berlin"), pa.timestamp("us", "UTC")], pa.timestamp("us", "UTC")),
    ([pa.string(), pa.large_string()], pa.string()),
    ([pa.binary(), pa.large_binary()], pa.binary()),
    ([pa.null(), pa.int16()], pa.int16()),
    ([pa.null(), pa.null()], pa.null()),
    ([pa.list_(pa.int32()), pa.large_list(pa.int64())], pa.list_(pa.int64())),
    ([pa.struct([("a", pa.int32())]), pa.struct([("a", pa.int64()), ("b", pa.string())])],
     pa.struct([("a", pa.int64()), ("b", pa.string())])),
])
def test_lossless_lattice(sources, target):
    assert merge_many(sources) == target


@pytest.mark.parametrize("sources", [
    [pa.string(), pa.int64()],
    [pa.float64(), pa.decimal128(12, 2)],
    [pa.int64(), pa.float64()],
    [pa.timestamp("ms"), pa.timestamp("ms", "UTC")],
    [pa.date32(), pa.timestamp("us", "UTC")],
    [pa.decimal128(38, 0), pa.decimal128(38, 38)],
    [pa.bool_(), pa.int32()],
    [pa.list_(pa.int32()), pa.int32()],
])
def test_conflicts(sources):
    with pytest.raises(Conflict):
        merge_many(sources)


def test_integer_decimal_lattice_independent_of_order():
    inputs = [pa.int64(), pa.uint64(), pa.decimal128(21, 2)]
    assert {merge_many(p) for p in itertools.permutations(inputs)} == {pa.decimal128(22, 2)}


@pytest.mark.parametrize("kind", [pa.time64("us"), pa.duration("ms"), pa.map_(pa.string(), pa.int32()),
                                 pa.decimal256(50, 2), pa.decimal128(6, -2), pa.list_(pa.int32(), 3)])
def test_unsupported_types_explicit(kind):
    with pytest.raises(UnifyError):
        canonical(kind)


def test_stringify_and_strict():
    assert merge_many([pa.string(), pa.int64()], "stringify-conflicts") == pa.string()
    with pytest.raises(Conflict):
        merge_many([pa.int32(), pa.int64()], "strict")
    with pytest.raises(Conflict, match="portably"):
        merge_many([pa.float64(), pa.decimal128(12, 2)], "stringify-conflicts")
