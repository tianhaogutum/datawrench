"""Versioned reading recipes, reusing DuckDB's own parameter vocabulary."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .audit import AuditError, AuditOptions, canonical_type

MAX_JSON_BYTES = 4 * 1024 * 1024


def read_json_object(path: str | Path) -> dict[str, Any]:
    """Read a bounded JSON object; duplicate keys must not silently replace types."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise AuditError(f"Duplicate JSON key: {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise AuditError(f"Non-finite JSON number is not allowed: {value}")

    try:
        with Path(path).expanduser().open("rb") as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise AuditError("JSON schema/config exceeds the 4 MiB limit")
        result = json.loads(
            data.decode("utf-8-sig"), object_pairs_hook=object_pairs, parse_constant=constant
        )
    except AuditError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise AuditError(f"Cannot read JSON {path}: {error}") from error
    if not isinstance(result, dict):
        raise AuditError("JSON schema/config must contain an object")
    return result


def load_config(path: str | Path) -> AuditOptions:
    """Load a conservative recipe. Its source hash is provenance, not a file lock."""
    config = read_json_object(path)
    if config.get("format") != "csv-rewind.duckdb" or type(config.get("version")) is not int:
        raise AuditError("Expected a csv-rewind.duckdb reading configuration")
    if config["version"] != 1:
        raise AuditError(f"Unsupported reading configuration version: {config['version']}")
    if config.get("timezone") != "UTC":
        raise AuditError("Reading configurations must use UTC")
    parameters = config.get("parameters")
    if not isinstance(parameters, dict):
        raise AuditError("Reading configuration parameters must be an object")
    allowed = {
        "auto_detect",
        "delim",
        "quote",
        "escape",
        "new_line",
        "skip",
        "comment",
        "header",
        "encoding",
        "strict_mode",
        "ignore_errors",
        "null_padding",
        "hive_partitioning",
        "dateformat",
        "timestampformat",
        "columns",
        "force_not_null",
        "nullstr",
        "allow_quoted_nulls",
    }
    unknown = set(parameters) - allowed
    if unknown:
        raise AuditError(f"Unknown reading parameter(s): {', '.join(sorted(unknown))}")
    for key, expected in {
        "auto_detect": False,
        "ignore_errors": False,
        "null_padding": False,
        "hive_partitioning": False,
        "strict_mode": True,
    }.items():
        if key in parameters and parameters[key] is not expected:
            raise AuditError(f"Reading configuration requires {key}={expected}")
    if parameters.get("comment", "") != "":
        raise AuditError("Reading configurations cannot discard comments")
    columns = parameters.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise AuditError("Reading configuration needs an ordered, non-empty columns object")
    types = {name: canonical_type(value) for name, value in columns.items()}
    names = list(types)
    if parameters.get("force_not_null") != names:
        raise AuditError("Reading configuration must force_not_null for every column in order")
    for key in ("delim", "quote", "escape", "encoding"):
        if not isinstance(parameters.get(key), str):
            raise AuditError(f"Reading parameter {key} must be a string")
    if type(parameters.get("header")) is not bool:
        raise AuditError("Reading parameter header must be a boolean")
    skip = parameters.get("skip", 0)
    if type(skip) is not int or skip < 0:
        raise AuditError("Reading parameter skip must be a non-negative integer")
    newline = parameters.get("new_line", config.get("sniffed_newline"))
    if newline not in (r"\n", r"\r\n", r"\r"):
        raise AuditError("Reading configuration needs an LF, CRLF or CR newline spelling")
    for key in ("dateformat", "timestampformat"):
        if key in parameters and not isinstance(parameters[key], str):
            raise AuditError(f"Reading parameter {key} must be a string")
    if "nullstr" in parameters:
        nulls = parameters["nullstr"]
        if not (
            isinstance(nulls, str)
            or isinstance(nulls, list)
            and nulls
            and all(isinstance(v, str) for v in nulls)
        ):
            raise AuditError("Reading parameter nullstr must be a string or non-empty string list")
    if "allow_quoted_nulls" in parameters and type(parameters["allow_quoted_nulls"]) is not bool:
        raise AuditError("Reading parameter allow_quoted_nulls must be a boolean")
    origin_hash = config.get("source_sha256")
    if origin_hash is not None and not (
        isinstance(origin_hash, str) and re.fullmatch(r"[0-9a-f]{64}", origin_hash)
    ):
        raise AuditError("Configuration source_sha256 must be a lowercase SHA-256 hex digest")
    engine = config.get("engine_version")
    if engine is not None and not (isinstance(engine, str) and len(engine) <= 64):
        raise AuditError("Configuration engine_version must be a short string")
    return AuditOptions(
        types=types,
        header=parameters["header"],
        delimiter=parameters["delim"],
        quote=parameters["quote"],
        escape=parameters["escape"],
        new_line=newline,
        skip=skip,
        encoding=parameters["encoding"],
        date_format=parameters.get("dateformat"),
        timestamp_format=parameters.get("timestampformat"),
        # The frozen import forces every field non-null. Re-auditing its VARCHAR
        # empty strings must not propose the default empty-string null coercion.
        null_values=(),
        expected_columns=tuple(names),
        baseline_source_sha256=origin_hash,
        baseline_engine_version=engine,
    )
