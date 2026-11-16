"""Generate tiny synthetic, redistributable Parquet fixtures; no external data."""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def make_dataset(destination, scenario="compatible"):
    out = Path(destination)
    out.mkdir(parents=True, exist_ok=False)
    if scenario == "conflict":
        pq.write_table(pa.table({"value": pa.array([1, None], type=pa.int64())}), out / "part-0.parquet")
        pq.write_table(pa.table({"value": pa.array(["001", "x"], type=pa.string())}), out / "part-1.parquet")
        return
    if scenario in ("float-decimal", "timezone"):
        if scenario == "float-decimal":
            a, b = pa.array([1.25], pa.float64()), pa.array([Decimal("1.25")], pa.decimal128(12, 2))
        else:
            a = pa.array([datetime(2026, 1, 1)], pa.timestamp("us"))
            b = pa.array([datetime(2026, 1, 1)], pa.timestamp("us", tz="UTC"))
        pq.write_table(pa.table({"value": a}), out / "part-0.parquet")
        pq.write_table(pa.table({"value": b}), out / "part-1.parquet")
        return
    first = pa.table({
        "id": pa.array([1, 2, 3], pa.int32()),
        "amount": pa.array([10, None, -2], pa.int32()),
        "score": pa.array([1.5, None, 2.25], pa.float32()),
        "at": pa.array([0, 1_700_000_000_001, None], pa.timestamp("ms")),
        "profile": pa.array([{"age": 20}, None, {"age": None}], pa.struct([("age", pa.int32())])),
        "tags": pa.array([[1, None], None, []], pa.list_(pa.int32())),
    })
    second = pa.table({
        "id": pa.array([2**40, 5], pa.int64()),
        "amount": pa.array([Decimal("2.50"), Decimal("-12.30")], pa.decimal128(12, 2)),
        "score": pa.array([2.125, float("nan")], pa.float64()),
        "at": pa.array([1_700_000_000_000_123, 1], pa.timestamp("us")),
        "profile": pa.array([{"age": 30, "city": "Berlin"}, {"age": None, "city": None}],
                            pa.struct([("age", pa.int64()), ("city", pa.string())])),
        "tags": pa.array([[2**40], [2, 3]], pa.list_(pa.int64())),
        "note": pa.array(["new", None]),
    })
    for i, table in enumerate((first, second)):
        directory = out / f"region={'eu' if i == 0 else 'us'}" / f"day=2026-11-{11+i}" if scenario == "hive" else out
        directory.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, directory / f"part-{i}.parquet", row_group_size=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination")
    parser.add_argument("--scenario", choices=("compatible", "conflict", "float-decimal", "timezone", "hive"), default="compatible")
    args = parser.parse_args()
    make_dataset(args.destination, args.scenario)
