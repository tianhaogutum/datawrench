"""Conservative, schema-only least upper bounds. No data-dependent guesses."""
from __future__ import annotations

import hashlib
import json
import pyarrow as pa


class UnifyError(Exception):
    """Invalid input or a deliberately unsupported conversion."""


class Conflict(UnifyError):
    pass


def is_list(t):
    return pa.types.is_list(t) or pa.types.is_large_list(t)


def supported(t):
    if pa.types.is_struct(t):
        names = [f.name for f in t]
        if len(set(names)) != len(names):
            raise UnifyError("duplicate struct field names")
        for f in t:
            supported(f.type)
    elif is_list(t):
        supported(t.value_type)
    elif pa.types.is_decimal(t):
        if t.precision > 38 or t.scale < 0 or t.scale > t.precision:
            raise UnifyError(f"unsupported decimal: {t}; require 0 <= scale <= precision <= 38")
    elif not (pa.types.is_null(t) or pa.types.is_boolean(t) or pa.types.is_integer(t)
              or t in (pa.float32(), pa.float64(), pa.string(), pa.large_string(), pa.binary(), pa.large_binary())
              or pa.types.is_date(t) or pa.types.is_timestamp(t)):
        raise UnifyError(f"unsupported Arrow type: {t}")


def canonical(t):
    supported(t)
    if pa.types.is_large_string(t):
        return pa.string()
    if pa.types.is_large_binary(t):
        return pa.binary()
    if pa.types.is_date(t):
        return pa.date32()
    if pa.types.is_struct(t):
        return pa.struct([pa.field(f.name, canonical(f.type)) for f in t])
    if is_list(t):
        return pa.list_(canonical(t.value_type))
    if pa.types.is_timestamp(t):
        return pa.timestamp("ms" if t.unit == "s" else t.unit, tz="UTC" if t.tz else None)
    return t


def integer_bounds(t):
    bits = t.bit_width
    return (0, 2**bits - 1) if pa.types.is_unsigned_integer(t) else (-2**(bits-1), 2**(bits-1)-1)


def merge_many(types, policy="widen"):
    # Merge all integer/decimal inputs together so the result is independent of file order.
    ts = [canonical(t) for t in types if not pa.types.is_null(t)]
    if not ts:
        return pa.null()
    if all(t == ts[0] for t in ts):
        return ts[0]
    if policy == "strict":
        raise Conflict("strict policy rejects differing types: " + ", ".join(map(str, ts)))
    if all(pa.types.is_struct(t) for t in ts):
        names = list(dict.fromkeys(f.name for t in ts for f in t))
        fields = []
        for n in names:
            try:
                child = merge_many([t.field(n).type for t in ts if n in [f.name for f in t]], policy)
            except UnifyError as exc:
                raise Conflict(f"struct field {n!r}: {exc}") from exc
            fields.append(pa.field(n, child))
        return pa.struct(fields)
    if all(is_list(t) for t in ts):
        return pa.list_(merge_many([t.value_type for t in ts], policy))
    if all(pa.types.is_integer(t) for t in ts):
        low = min(integer_bounds(t)[0] for t in ts)
        high = max(integer_bounds(t)[1] for t in ts)
        for width in (8, 16, 32, 64):
            candidate = getattr(pa, ("uint" if low >= 0 else "int") + str(width))()
            lo, hi = integer_bounds(candidate)
            if lo <= low and high <= hi:
                return candidate
        return pa.decimal128(20, 0)  # int64 + uint64
    if all(pa.types.is_integer(t) or pa.types.is_decimal(t) for t in ts):
        scale = max([t.scale for t in ts if pa.types.is_decimal(t)] or [0])
        digits = []
        for t in ts:
            if pa.types.is_decimal(t):
                digits.append(t.precision - t.scale)
            else:
                lo, hi = integer_bounds(t)
                digits.append(len(str(max(abs(lo), abs(hi)))))
        precision = max(digits) + scale
        if precision <= 38:
            return pa.decimal128(precision, scale)
    elif all(pa.types.is_floating(t) or pa.types.is_integer(t) for t in ts):
        # Float64 exactly contains all int32/uint32 values, but not all int64/uint64.
        if all(not pa.types.is_integer(t) or t.bit_width <= 32 for t in ts):
            return pa.float64()
    elif all(pa.types.is_date(t) or pa.types.is_timestamp(t) for t in ts):
        timestamps = [t for t in ts if pa.types.is_timestamp(t)]
        zones = {bool(t.tz) for t in timestamps}
        if len(zones) <= 1 and not (True in zones and any(pa.types.is_date(t) for t in ts)):
            unit = max((t.unit for t in timestamps), key=("s", "ms", "us", "ns").index)
            return pa.timestamp(unit, tz="UTC" if True in zones else None)
    if policy == "stringify-conflicts":
        # Keep this deterministic across engines: generic float/temporal/complex string
        # formatting is engine-dependent, so those cases must fail explicitly.
        if all(pa.types.is_integer(t) or pa.types.is_decimal(t) or pa.types.is_boolean(t)
               or pa.types.is_string(t) for t in ts):
            return pa.string()
        raise Conflict("cannot stringify this conflict portably (only string/integer/decimal/boolean): " + ", ".join(map(str, ts)))
    raise Conflict("no lossless common type: " + ", ".join(map(str, ts)))


def type_spec(t):
    if pa.types.is_struct(t):
        return {"struct": [{"name": f.name, "type": type_spec(f.type)} for f in t]}
    if is_list(t):
        return {"list": type_spec(t.value_type), "large": pa.types.is_large_list(t)}
    return str(t)


def schema_spec(schema):
    return [{"name": f.name, "type": type_spec(f.type)} for f in schema]


def fingerprint(schema):
    # Ignore field metadata and nullability, retain physical field order and types.
    return hashlib.sha256(json.dumps(schema_spec(schema), sort_keys=True).encode()).hexdigest()


def flatten_parts(schema):
    """Key by literal field names; None uniquely identifies a list element."""
    result = {}

    def visit(t, path):
        result[path] = t
        if pa.types.is_struct(t):
            for field in t:
                visit(field.type, path + (field.name,))
        elif is_list(t):
            visit(t.value_type, path + (None,))

    for field in schema:
        visit(field.type, (field.name,))
    return result


def pointer_path(parts):
    return "".join("/[]" if part is None else "/" + part.replace("~", "~0").replace("/", "~1")
                   for part in parts)


def flatten(schema, prefix=""):
    """Display paths for one schema; use flatten_parts to compare field identities."""
    return {prefix + pointer_path(parts): t for parts, t in flatten_parts(schema).items()}
