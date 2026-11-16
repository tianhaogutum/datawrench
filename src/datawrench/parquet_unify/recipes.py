"""Generate standalone engine programs; all identifiers, literals and paths are quoted."""
from __future__ import annotations

import pyarrow as pa
from .types import UnifyError, is_list


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def sql_type(t):
    names = {pa.null(): "INTEGER", pa.bool_(): "BOOLEAN", pa.int8(): "TINYINT", pa.int16(): "SMALLINT",
             pa.int32(): "INTEGER", pa.int64(): "BIGINT", pa.uint8(): "UTINYINT", pa.uint16(): "USMALLINT",
             pa.uint32(): "UINTEGER", pa.uint64(): "UBIGINT", pa.float32(): "FLOAT", pa.float64(): "DOUBLE",
             pa.string(): "VARCHAR", pa.binary(): "BLOB", pa.date32(): "DATE"}
    if t in names:
        return names[t]
    if pa.types.is_decimal(t):
        return f"DECIMAL({t.precision},{t.scale})"
    if pa.types.is_timestamp(t):
        if t.tz:
            if t.unit == "ns":
                raise UnifyError("DuckDB cannot preserve nanosecond timezone-aware timestamps; rewrite/plan supported, recipe/verify unavailable")
            return "TIMESTAMPTZ"
        return {"ms": "TIMESTAMP_MS", "us": "TIMESTAMP", "ns": "TIMESTAMP_NS"}[t.unit]
    if pa.types.is_struct(t):
        return "STRUCT(" + ", ".join(ident(f.name) + " " + sql_type(f.type) for f in t) + ")"
    if is_list(t):
        return sql_type(t.value_type) + "[]"
    raise UnifyError(f"no DuckDB mapping: {t}")


def polars_type(t):
    names = {pa.null(): "Null", pa.bool_(): "Boolean", pa.int8(): "Int8", pa.int16(): "Int16",
             pa.int32(): "Int32", pa.int64(): "Int64", pa.uint8(): "UInt8", pa.uint16(): "UInt16",
             pa.uint32(): "UInt32", pa.uint64(): "UInt64", pa.float32(): "Float32", pa.float64(): "Float64",
             pa.string(): "String", pa.binary(): "Binary", pa.date32(): "Date"}
    if t in names:
        return "pl." + names[t]
    if pa.types.is_decimal(t):
        return f"pl.Decimal({t.precision}, {t.scale})"
    if pa.types.is_timestamp(t):
        return f"pl.Datetime({t.unit!r}, {t.tz!r})"
    if pa.types.is_struct(t):
        return "pl.Struct([" + ", ".join(f"pl.Field({f.name!r}, {polars_type(f.type)})" for f in t) + "])"
    if is_list(t):
        return f"pl.List({polars_type(t.value_type)})"
    raise UnifyError(f"no Polars mapping: {t}")


def sql_cast(expr, source, target, depth=0):
    if source is None or pa.types.is_null(source):
        return f"CAST(NULL AS {sql_type(target)})"
    if pa.types.is_struct(source) and pa.types.is_struct(target):
        fields = []
        for f in target:
            present = source.get_field_index(f.name) >= 0
            child = sql_cast(f"({expr}).{ident(f.name)}", source.field(f.name).type if present else None, f.type, depth+1)
            fields.append(f"{ident(f.name)} := {child}")
        expr = f"CASE WHEN {expr} IS NULL THEN NULL ELSE struct_pack({', '.join(fields)}) END"
    elif is_list(source) and is_list(target):
        var = "_pu_element_" + str(depth)
        expr = f"list_transform({expr}, {var} -> {sql_cast(var, source.value_type, target.value_type, depth+1)})"
    return f"CAST({expr} AS {sql_type(target)})"


def polars_cast(expr, source, target):
    dtype = polars_type(target)
    if source is None:
        return f"pl.lit(None, dtype={dtype})"
    if pa.types.is_null(source):
        return f"({expr}).cast({dtype}, strict=True)"
    if pa.types.is_struct(source) and pa.types.is_struct(target):
        fields = []
        # Struct field strings also expand patterns; temporarily give every
        # physical field a safe, unique name before selecting one literally.
        safe_names = [f"_pu_field_{index}" for index in range(len(source))]
        safe_struct = f"({expr}).struct.rename_fields({safe_names!r})"
        for f in target:
            index = source.get_field_index(f.name)
            child_expr = f"({safe_struct}).struct.field({safe_names[index]!r})" if index >= 0 else "pl.lit(None)"
            child = polars_cast(child_expr, source.field(index).type if index >= 0 else None, f.type)
            fields.append(f"({child}).alias({f.name!r})")
        expr = f"pl.when(({expr}).is_null()).then(pl.lit(None, dtype={dtype})).otherwise(pl.struct([{', '.join(fields)}]))"
    elif is_list(source) and is_list(target):
        child = polars_cast("pl.element()", source.value_type, target.value_type)
        expr = f"({expr}).list.eval({child})"
    return f"({expr}).cast({dtype}, strict=True)"


def validate_names(schema):
    names = [f.name for f in schema]
    if any(not n or '\x00' in n for n in names) or len({n.casefold() for n in names}) != len(names):
        raise UnifyError("DuckDB recipes require nonempty, NUL-free, case-insensitively unique field names")
    for field in schema:
        t = field.type
        while is_list(t):
            t = t.value_type
        if pa.types.is_struct(t):
            if not len(t):
                raise UnifyError("DuckDB recipes do not support empty structs")
            validate_names(t)


def recipe_groups(catalog):
    # Split schema groups at partition constants, so no engine infers Hive types.
    groups = {}
    for fp, entries in catalog.groups().items():
        for e in entries:
            if any(c in str(e.path) for c in '*?[]'):
                raise UnifyError(f"glob metacharacters in paths are unsupported by DuckDB: {e.path}")
            groups.setdefault((fp, tuple(e.partitions.items())), []).append(e)
    return list(groups.values())


def duckdb_recipe(catalog, target):
    validate_names(target)
    selects = []
    for entries in recipe_groups(catalog):
        entry = entries[0]
        validate_names(entry.schema)
        expressions = []
        for f in target:
            present = f.name in entry.schema.names
            source = entry.schema.field(f.name).type if present else None
            expr = literal(entry.partitions[f.name]) if f.name in entry.partitions else ident(f.name)
            expressions.append(sql_cast(expr, source, f.type) + " AS " + ident(f.name))
        files = "[" + ", ".join(literal(str(e.path)) for e in entries) + "]"
        selects.append("SELECT " + ", ".join(expressions) + f"\nFROM read_parquet({files}, hive_partitioning=false, union_by_name=false)")
    return "-- datawrench parquet-unify: local snapshot\nSET threads=2;\nSET TimeZone='UTC';\n" + "\nUNION ALL BY NAME\n".join(selects) + ";\n"


def polars_recipe(catalog, target):
    lines = ["# Generated by datawrench parquet-unify. Run as Python; use result.collect(engine='streaming').",
             "import os", "os.environ['POLARS_MAX_THREADS'] = '2'", "import polars as pl", "import polars.selectors as cs",
             "if pl.thread_pool_size() > 2:",
             "    raise RuntimeError('Polars already initialized with >2 threads; run in a fresh process')",
             "_groups = []"]
    for entries in recipe_groups(catalog):
        entry = entries[0]
        expressions = []
        for f in target:
            present = f.name in entry.schema.names
            source = entry.schema.field(f.name).type if present else None
            expr = f"pl.lit({entry.partitions[f.name]!r}, dtype=pl.String)" if f.name in entry.partitions else f"cs.by_name({f.name!r})"
            expressions.append(f"({polars_cast(expr, source, f.type)}).alias({f.name!r})")
        files = repr([str(e.path) for e in entries])
        lines.append(f"_groups.append(pl.scan_parquet({files}, hive_partitioning=False, glob=False).select([{', '.join(expressions)}]))")
    lines += ["result = pl.concat(_groups, how='vertical')", "if __name__ == '__main__':", "    print(result.collect(engine='streaming'))"]
    return "\n".join(lines) + "\n"
