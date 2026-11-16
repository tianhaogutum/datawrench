"""Row-group normalization and order-independent, duplicate-sensitive checksums."""
from __future__ import annotations

import hashlib
import json
import math
import struct
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .types import UnifyError, canonical, is_list, schema_spec


def normalize_array(array, target):
    if isinstance(array, pa.ChunkedArray):
        array = array.combine_chunks()
    source = array.type
    if pa.types.is_null(source):
        return pa.nulls(len(array), type=target)
    if pa.types.is_null(target):
        if array.null_count != len(array):
            raise UnifyError("non-null engine values for a null-only target")
        return pa.nulls(len(array))
    if pa.types.is_struct(source) and pa.types.is_struct(target):
        children = [normalize_array(array.field(f.name), f.type) if source.get_field_index(f.name) >= 0
                    else pa.nulls(len(array), type=f.type) for f in target]
        return pa.StructArray.from_arrays(children, fields=list(target), mask=array.is_null())
    if is_list(source) and is_list(target):
        start, end = array.offsets[0].as_py(), array.offsets[-1].as_py()
        offsets = pc.cast(pc.subtract(array.offsets, start), pa.int32(), safe=True)
        return pa.ListArray.from_arrays(offsets,
                                        normalize_array(array.values.slice(start, end-start), target.value_type),
                                        mask=array.is_null())
    return pc.cast(array, target, safe=True)


def normalize_table(table, target):
    return pa.Table.from_arrays([normalize_array(table[f.name], f.type) if f.name in table.column_names
                                else pa.nulls(table.num_rows, type=f.type) for f in target], schema=target)


def entry_batches(entry, target):
    with pq.ParquetFile(entry.path) as reader:
        for i in range(reader.num_row_groups):
            table = reader.read_row_group(i, use_threads=False)
            for key, value in entry.partitions.items():
                table = table.append_column(key, pa.array([value] * table.num_rows, type=pa.string()))
            yield normalize_table(table, target)


def scalar_value(scalar, t):
    if not scalar.is_valid:
        return None
    if pa.types.is_struct(t):
        return [scalar_value(scalar[f.name], f.type) for f in t]
    if is_list(t):
        return [scalar_value(x, t.value_type) for x in scalar.values]
    if pa.types.is_timestamp(t):
        return scalar.value  # preserves ns, unlike datetime conversion
    if pa.types.is_date(t):
        # Normalized date32 spans all int32 epoch days, beyond Python's dates.
        # Keep historical ISO payloads where representable; escape the rest.
        if pa.types.is_date32(t) and not -719162 <= scalar.value <= 2932896:
            return {"date32_days": scalar.value}
        return scalar.as_py().isoformat()
    value = scalar.as_py()
    if pa.types.is_floating(t):
        if math.isnan(value):
            return "NaN"
        return struct.pack(">d", 0.0 if value == 0 else value).hex()
    if pa.types.is_decimal(t):
        return str(value)
    if pa.types.is_binary(t):
        return value.hex()
    return value


class Summary:
    def __init__(self, schema):
        self.schema = schema
        self.rows = 0
        self.sums = {f.name: [0, 0] for f in schema}

    def add(self, table):
        table = normalize_table(table, self.schema)
        self.rows += table.num_rows
        modulus = 1 << 256
        for field in self.schema:
            pair = self.sums[field.name]
            for scalar in table[field.name]:
                payload = json.dumps(scalar_value(scalar, field.type), ensure_ascii=True, separators=(",", ":")).encode()
                value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
                pair[0] = (pair[0] + value) % modulus
                pair[1] = (pair[1] + value * value) % modulus

    def report(self):
        return {"rows": self.rows, "schema": schema_spec(self.schema),
                "checksums": {name: hashlib.sha256(f"{self.rows}:{a:064x}:{b:064x}".encode()).hexdigest()
                              for name, (a, b) in self.sums.items()}}


class RowSummary:
    """Multiset of ordered planned-type tuples; retain no rows or row hashes."""

    def __init__(self, schema):
        self.schema = schema
        self.header = ["parquet-unify-row-v1", schema_spec(schema)]
        self.rows = 0
        self.total = 0
        self.squares = 0

    def add(self, table):
        table = normalize_table(table, self.schema)
        self.rows += table.num_rows
        modulus = 1 << 256
        for row in zip(*(table[f.name] for f in self.schema)):
            values = [scalar_value(scalar, field.type) for scalar, field in zip(row, self.schema)]
            payload = json.dumps([*self.header, values], ensure_ascii=True, separators=(",", ":")).encode()
            value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
            self.total = (self.total + value) % modulus
            self.squares = (self.squares + value * value) % modulus

    def digest(self):
        return hashlib.sha256(f"{self.rows}:{self.total:064x}:{self.squares:064x}".encode()).hexdigest()


class OrderedRowSummary:
    """Streaming sequence of canonical tuples, independent of batch/file framing."""

    def __init__(self, schema):
        self.schema = schema
        self.rows = 0
        self._hash = hashlib.sha256()
        self._json_frame(b"H", ["parquet-unify-ordered-row-v1", schema_spec(schema)])

    def _json_frame(self, tag, value):
        payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
        self._hash.update(tag + len(payload).to_bytes(8, "big"))
        self._hash.update(payload)

    def add(self, table):
        table = normalize_table(table, self.schema)
        self.rows += table.num_rows
        for row in zip(*(table[f.name] for f in self.schema)):
            self._json_frame(b"R", [scalar_value(scalar, field.type)
                                    for scalar, field in zip(row, self.schema)])

    def digest(self):
        final = self._hash.copy()
        count = str(self.rows).encode("ascii")
        final.update(b"C" + len(count).to_bytes(8, "big") + count)
        return final.hexdigest()


def source_summary(catalog, target):
    summary = Summary(target)
    for entry in catalog.entries:
        for table in entry_batches(entry, target):
            summary.add(table)
    return summary.report()


def compatible_schema(actual, target):
    """Permit documented engine carriers, never missing/reordered fields or numeric narrowing."""
    if actual.names != target.names:
        return False

    def compatible(a, t):
        if pa.types.is_null(t):  # DuckDB represents an all-NULL column as INTEGER.
            return True  # normalize_array separately enforces all values are NULL.
        if pa.types.is_struct(a) and pa.types.is_struct(t):
            return [f.name for f in a] == [f.name for f in t] and all(compatible(x.type, y.type) for x, y in zip(a, t))
        if is_list(a) and is_list(t):
            return compatible(a.value_type, t.value_type)
        if pa.types.is_timestamp(a) and pa.types.is_timestamp(t) and a.tz and t.tz:
            return a.unit in ("ms", "us", "ns")  # DuckDB uses UTC microseconds for TIMESTAMPTZ.
        return canonical(a) == t

    return all(compatible(a.type, t.type) for a, t in zip(actual, target))
