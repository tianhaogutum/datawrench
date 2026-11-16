"""Reusable validators. Schemas are interpreted, never compiled into Python code."""
from __future__ import annotations

from jsonschema import Draft7Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import NoSuchResource

from .io import InputError

DIALECTS = {
    "http://json-schema.org/draft-04/schema",
    "http://json-schema.org/draft-06/schema",
    "http://json-schema.org/draft-07/schema",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
}


def deny_retrieval(uri):
    raise NoSuchResource(ref=uri)


def secure_schema(schema):
    if not isinstance(schema, (dict, bool)):
        raise InputError("schema must be an object or boolean", "TC004")
    # Walk schema keywords, not arbitrary data in enum/default/examples.
    single = {"additionalProperties", "additionalItems", "contains", "not", "if", "then", "else", "propertyNames", "unevaluatedProperties", "unevaluatedItems", "contentSchema"}
    maps = {"properties", "patternProperties", "definitions", "$defs", "dependentSchemas"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    def walk(node):
        if not isinstance(node, dict):
            return
        for key, val in node.items():
            if key == "$schema" and val.rstrip("#") not in DIALECTS:
                raise InputError("unsupported JSON Schema dialect", "TC004")
            if key in {"$dynamicRef", "$recursiveRef"}:
                raise InputError("dynamic/recursive references are not supported", "TC004")
            if key == "$ref" and (not isinstance(val, str) or not val.startswith("#")):
                raise InputError("only local fragment $ref is supported; network/file retrieval is disabled", "TC004")
            if key in maps and isinstance(val, dict):
                for child in val.values(): walk(child)
            elif key in single:
                walk(val)
            elif key in arrays or key == "items":
                for child in val if isinstance(val, list) else [val]: walk(child)
            elif key == "dependencies" and isinstance(val, dict):
                for child in val.values(): walk(child)
    try:
        walk(schema)
        cls = validators.validator_for(schema, default=Draft7Validator)
        cls.check_schema(schema)
    except (SchemaError, RecursionError, TypeError, AttributeError, ValueError) as exc:
        raise InputError("invalid or too deeply nested JSON Schema", "TC004") from exc
    return cls


def compile_schema(schema, strict=False):
    cls = secure_schema(schema)
    # Registry refuses retrieval even for references hidden in future keywords.
    return cls(schema, registry=Registry(retrieve=deny_retrieval),
               format_checker=FormatChecker(["date-time"]) if strict else None)
