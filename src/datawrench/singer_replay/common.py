"""Strict JSON and the deliberately bounded Singer/JSON Schema dialect."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

MAX_LINE = 16 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024
MAX_MESSAGES_BYTES = 1024 * 1024 * 1024


class ReplayError(Exception):
    """An actionable, safe-to-display input error."""


def require(condition, message):
    if not condition:
        raise ReplayError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON object key")
        result[key] = value
    return result


def decode(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReplayError("invalid JSON (UTF-8, unique keys and finite numbers required)") from exc


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def read_json(path):
    with Path(path).open("rb") as src:
        data = src.read(MAX_JSON + 1)
    require(len(data) <= MAX_JSON, "JSON file exceeds 16 MiB")
    return decode(data)


def lines(src):
    total = 0
    for number in range(1, MAX_MESSAGES_BYTES + 1):
        raw = src.readline(MAX_LINE + 1)
        if not raw:
            return
        total += len(raw)
        require(len(raw) <= MAX_LINE, f"message {number} exceeds 16 MiB")
        require(total <= MAX_MESSAGES_BYTES, "messages exceed 1 GiB uncompressed")
        require(raw.strip(), f"empty message at line {number}")
        try:
            msg = decode(raw)
            require(isinstance(msg, dict), "message must be an object")
            yield msg
        except ReplayError as exc:
            raise ReplayError(f"message {number}: {exc}") from exc


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d{1,6})?"
        r"(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value),
        "timestamp must be RFC3339 with timezone and at most 6 fractional digits")
    try:
        # fromisoformat normalizes offsets such as +00:60; bound them above.
        return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ReplayError("invalid RFC3339 timestamp") from exc


def render_time(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
# No references, compositions, conditional schemas or pattern properties: projection
# cannot safely rewrite their semantics. Refuse them rather than silently weaken them.
SCHEMA_KEYS = {"type", "properties", "required", "items", "additionalProperties",
               "format", "description", "title", "$schema", "examples", "default"}


def check_schema(schema, depth=0):
    require(depth <= 32 and isinstance(schema, dict), "schema must be an object, depth <=32")
    require(not (set(schema) - SCHEMA_KEYS), "unsupported JSON Schema keyword")
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    require(types and all(isinstance(t, str) and t in TYPES for t in types),
            "schema requires explicit supported type(s)")
    if "object" in types:
        props = schema.get("properties", {})
        require(isinstance(props, dict), "properties must be an object")
        require(schema.get("additionalProperties", False) is False,
                "additionalProperties:true/schema is unsupported; declare fields explicitly")
        required = schema.get("required", [])
        require(isinstance(required, list) and all(isinstance(k, str) and k in props for k in required),
                "required must name declared properties")
        for key, child in props.items():
            require(isinstance(key, str) and key, "property names must be nonempty")
            check_schema(child, depth + 1)
    if "array" in types:
        require("items" in schema, "arrays require an items schema")
        check_schema(schema["items"], depth + 1)
    if "format" in schema:
        require(isinstance(schema["format"], str), "format must be a string")


def validate(value, schema, depth=0):
    require(depth <= 32, "record exceeds nesting depth 32")
    kinds = schema["type"]
    kinds = kinds if isinstance(kinds, list) else [kinds]
    matches = {"null": value is None, "boolean": isinstance(value, bool),
               "integer": type(value) is int, "number": type(value) in (int, float),
               "string": isinstance(value, str), "object": isinstance(value, dict),
               "array": isinstance(value, list)}
    require(any(matches[t] for t in kinds), "record value does not match schema type")
    if isinstance(value, float):
        require(math.isfinite(value), "non-finite numeric value")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        require(not (set(value) - set(props)), "record contains undeclared properties")
        require(set(schema.get("required", [])) <= set(value), "record missing required properties")
        for key, child in value.items():
            validate(child, props[key], depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate(child, schema["items"], depth + 1)
    elif isinstance(value, str) and schema.get("format") == "date-time":
        timestamp(value)


def order_value(value, schema):
    require(value is not None and type(value) in (int, float, str),
            "replication values must be non-null strings or numbers")
    if schema.get("format") == "date-time":
        return timestamp(value)
    return value


def compare(left, right, schema):
    a, b = order_value(left, schema), order_value(right, schema)
    require((isinstance(a, str) == isinstance(b, str)) and
            (isinstance(a, datetime) == isinstance(b, datetime)), "incompatible bookmark types")
    return (a > b) - (a < b)
