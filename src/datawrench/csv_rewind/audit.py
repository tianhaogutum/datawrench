"""Compose DuckDB sniffing, text ingestion and casts; never parse CSV ourselves."""

from __future__ import annotations

import codecs
import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from glob import escape as glob_escape
from pathlib import Path
from typing import Any, TextIO

import duckdb


class AuditError(ValueError):
    """An input or engine error that prevents a complete audit."""


@dataclass(frozen=True)
class AuditOptions:
    types: dict[str, str] = field(default_factory=dict)
    header: bool | None = None
    delimiter: str | None = None
    quote: str | None = None
    escape: str | None = None
    new_line: str | None = None
    skip: int = 0
    date_format: str | None = None
    timestamp_format: str | None = None
    null_values: tuple[str, ...] = ("",)
    encoding: str = "utf-8"
    sample_size: int = 20_480
    max_examples: int = 20
    max_cell_chars: int = 512
    batch_size: int = 64
    memory_limit_mb: int = 128
    expected_columns: tuple[str, ...] | None = None
    baseline_source_sha256: str | None = None
    baseline_engine_version: str | None = None


_ALIASES = {
    "INT": "INTEGER",
    "INT4": "INTEGER",
    "INT8": "BIGINT",
    "INT2": "SMALLINT",
    "STRING": "VARCHAR",
    "TEXT": "VARCHAR",
    "BOOL": "BOOLEAN",
    "REAL": "FLOAT",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
    "TIMESTAMP WITHOUT TIME ZONE": "TIMESTAMP",
}
_INTEGER_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
    "BIGNUM",
}
_NUMERIC_TYPES = _INTEGER_TYPES | {"FLOAT", "DOUBLE"}
_TEMPORAL_TYPES = {
    "DATE",
    "TIME",
    "TIMESTAMP",
    "TIMESTAMP_S",
    "TIMESTAMP_MS",
    "TIMESTAMP_NS",
    "TIMESTAMPTZ",
}
_TYPES = _NUMERIC_TYPES | _TEMPORAL_TYPES | {"VARCHAR", "BOOLEAN"}


def canonical_type(value: str) -> str:
    """An intentionally small whitelist: type arguments can never become SQL."""
    if not isinstance(value, str):
        raise AuditError("Column types must be strings")
    if len(value) > 128:
        raise AuditError("A column type is too long (maximum 128 characters)")
    value = " ".join(value.upper().strip().split())
    value = _ALIASES.get(value, value)
    if value in _TYPES:
        return value
    match = re.fullmatch(r"(?:DECIMAL|NUMERIC)\(\s*(\d+)\s*,\s*(\d+)\s*\)", value)
    if match:
        precision, scale = map(int, match.groups())
        if 1 <= precision <= 38 and 0 <= scale <= precision:
            return f"DECIMAL({precision},{scale})"
    raise AuditError(
        f"Unsupported type {value!r}; use VARCHAR, BOOLEAN, a DuckDB integer/float, "
        "DATE/TIME/TIMESTAMP/TIMESTAMPTZ, or DECIMAL(precision,scale)"
    )


def sql_value(value: Any) -> str:
    """Encode only data literals; all identifiers and types are handled separately."""
    if isinstance(value, str):
        value = value.replace("\\", "\\\\").replace("'", "''")
        value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return "E'" + value + "'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(sql_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(sql_value(k) + ": " + sql_value(v) for k, v in value.items()) + "}"
    raise AuditError(f"Unsupported SQL parameter value: {type(value).__name__}")


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def read_expression(path: Path, parameters: dict[str, Any]) -> str:
    # SQL escaping alone does not disable DuckDB's glob expansion. A literal
    # supplier[1]*.csv must never select supplier1-extra.csv instead.
    args = [sql_value(glob_escape(str(path)))]
    args.extend(f"{key}={sql_value(value)}" for key, value in parameters.items())
    return "read_csv(" + ", ".join(args) + ")"


def fingerprint(path: Path, validate_encoding: str | None = None) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    decoder = codecs.getincrementaldecoder(validate_encoding)() if validate_encoding else None
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            size += len(block)
            digest.update(block)
            if decoder is not None:
                try:
                    decoder.decode(block)
                except UnicodeError as error:
                    raise AuditError(f"Invalid {validate_encoding} input: {error}") from error
    if decoder is not None:
        try:
            decoder.decode(b"", final=True)
        except UnicodeError as error:
            raise AuditError(f"Invalid {validate_encoding} input: {error}") from error
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def _sniff(connection: Any, path: Path, options: AuditOptions) -> dict[str, Any]:
    args: dict[str, Any] = {
        "sample_size": options.sample_size,
        "skip": options.skip,
        "comment": "",  # Never silently treat supplier records as comments.
        "encoding": options.encoding,
    }
    for name, value in {
        "header": options.header,
        "delim": options.delimiter,
        "quote": options.quote,
        "escape": options.escape,
        "new_line": options.new_line,
        "dateformat": options.date_format,
        "timestampformat": options.timestamp_format,
    }.items():
        if value is not None:
            args[name] = value

    def execute(arguments: dict[str, Any]) -> dict[str, Any]:
        query = "FROM sniff_csv(" + sql_value(glob_escape(str(path)))
        query += (
            ", " + ", ".join(f"{key}={sql_value(value)}" for key, value in arguments.items()) + ")"
        )
        result = connection.execute(query)
        return dict(
            zip([column[0] for column in result.description], result.fetchone(), strict=True)
        )

    try:
        return execute(args)
    except duckdb.Error:
        # The strict sniffer rejects some valid UTF-16/CR multiline inputs even
        # with the full producer dialect supplied. Retry only with every dialect
        # choice already fixed; actual ingestion still uses the strict reader.
        if any(
            value is None
            for value in (
                options.delimiter,
                options.quote,
                options.escape,
                options.new_line,
                options.header,
            )
        ):
            raise
        try:
            result = execute({**args, "strict_mode": False})
            result["RelaxedSniffRetry"] = True
            return result
        except duckdb.Error:
            # Reproduced: UTF-16, CR-only, headerless records with multiline
            # fields can defeat both sniffers although the configured reader
            # works. Bootstrap only the width with the established stdlib CSV
            # reader. There is deliberately no home-grown type inference.
            if not (
                options.header is False
                and options.new_line == r"\r"
                and options.encoding.lower().replace("_", "-") == "utf-16"
                and len(options.delimiter) == 1
                and len(options.quote) <= 1
                and len(options.escape) <= 1
            ):
                raise
            with path.open("r", encoding=options.encoding, newline="") as stream:
                try:
                    for _ in range(options.skip):
                        next(stream, None)
                    records = csv.reader(
                        stream,
                        delimiter=options.delimiter,
                        quotechar=options.quote or None,
                        quoting=csv.QUOTE_MINIMAL if options.quote else csv.QUOTE_NONE,
                        doublequote=options.escape == options.quote,
                        escapechar=(options.escape or None)
                        if options.escape != options.quote
                        else None,
                        strict=True,
                    )
                    first = next((row for row in records if row), [])
                except (csv.Error, UnicodeError) as error:
                    raise AuditError(
                        f"Cannot determine the first CSV record's width: {error}"
                    ) from error
            if not first:
                raise AuditError("No data records in the supplied headerless CSV") from None
            width = len(str(len(first) - 1))
            return {
                "Delimiter": options.delimiter,
                "Quote": options.quote,
                "Escape": options.escape,
                "NewLineDelimiter": options.new_line,
                "SkipRows": options.skip,
                "HasHeader": False,
                "Columns": [
                    {"name": f"column{i:0{width}d}", "type": "VARCHAR"} for i in range(len(first))
                ],
                "DateFormat": options.date_format,
                "TimestampFormat": options.timestamp_format,
                "InferenceUnavailable": True,
            }


def _parameters(sniff: dict[str, Any], options: AuditOptions) -> dict[str, Any]:
    def character(name: str) -> str:
        value = sniff.get(name)
        return "" if value in (None, "(empty)") else value

    # DuckDB expects the escaped spelling (two characters for \n), not a LF byte.
    newline = sniff["NewLineDelimiter"]
    result = {
        "auto_detect": False,
        "delim": sniff["Delimiter"],
        "quote": character("Quote"),
        "escape": character("Escape"),
        "new_line": newline,
        "skip": sniff["SkipRows"],
        "comment": "",
        "header": sniff["HasHeader"],
        "encoding": options.encoding,
        "ignore_errors": False,
        "null_padding": False,
        "hive_partitioning": False,
    }
    # The documented reader default is strict_mode=true. Explicitly setting it
    # with CRLF triggers zero rows/truncation in 2.0. Retain the fixed newline and
    # use that strict default. Malformed-row regressions guard it (UPSTREAM.md).
    # ISO is the native date parser's default. Explicit strptime in 1.4.x
    # mishandles infinity/BCE dates even though the native CSV parser accepts them.
    date_format = (
        options.date_format if options.expected_columns is not None else sniff.get("DateFormat")
    )
    timestamp_format = (
        options.timestamp_format
        if options.expected_columns is not None
        else sniff.get("TimestampFormat")
    )
    if date_format not in (None, "", "%Y-%m-%d"):
        result["dateformat"] = date_format
    if timestamp_format:
        result["timestampformat"] = timestamp_format
    return result


def _cast(raw: str, candidate: str, parameters: dict[str, Any]) -> str:
    if candidate == "DATE" and parameters.get("dateformat") not in (None, "%Y-%m-%d"):
        return f"TRY_CAST(try_strptime({raw}, {sql_value(parameters['dateformat'])}) AS DATE)"
    if candidate.startswith("TIMESTAMP") and parameters.get("timestampformat"):
        return (
            f"TRY_CAST(try_strptime({raw}, {sql_value(parameters['timestampformat'])}) "
            f"AS {candidate})"
        )
    return f"TRY_CAST({raw} AS {candidate})"


def _reason(raw: str, rendered: str | None, candidate: str, nulls: tuple[str, ...]) -> str:
    if raw in nulls:
        return "null_coercion"
    if rendered is None:
        return "parse_error"
    if candidate in _NUMERIC_TYPES or candidate.startswith("DECIMAL("):
        try:
            before, after = Decimal(raw), Decimal(rendered)
            if not (before.is_nan() and after.is_nan()) and before != after:
                return "numeric_value_change"
        except InvalidOperation:
            pass
        if re.match(r"^[+-]?0\d", raw.strip()):
            return "leading_zero_loss"
        if raw != raw.strip():
            return "whitespace_change"
        return "numeric_format_change"
    if candidate in _TEMPORAL_TYPES:
        return "temporal_format_change"
    if candidate == "BOOLEAN":
        return "boolean_format_change"
    return "text_change"


def _example_value(value: str | None, limit: int) -> dict[str, Any]:
    if value is None:
        return {"text": None}
    result: dict[str, Any] = {"text": value[:limit], "length": len(value)}
    if len(value) > limit:
        result.update(truncated=True, sha256=hashlib.sha256(value.encode("utf-8")).hexdigest())
    return result


def _validate_options(options: AuditOptions) -> None:
    for name in ("skip", "max_examples", "max_cell_chars"):
        if getattr(options, name) < 0:
            raise AuditError(f"{name} must be non-negative")
    if options.batch_size < 1 or options.memory_limit_mb < 32:
        raise AuditError("batch_size must be positive and memory_limit_mb must be at least 32")
    if options.sample_size != -1 and options.sample_size < 1:
        raise AuditError("sample_size must be positive or -1 for a full-file sniff")
    if not all(isinstance(value, str) for value in options.null_values):
        raise AuditError("Null tokens must be strings")
    if options.new_line not in (None, r"\n", r"\r\n", r"\r"):
        raise AuditError("new_line must be an escaped LF, CRLF or CR spelling")


def _disable_type_pushdown(connection: Any) -> list[str]:
    """A comparison CAST must run after CSV conversion, not replace its type."""
    available = {
        row[0] for row in connection.execute("SELECT name FROM duckdb_optimizers()").fetchall()
    }
    if "type_pushdown" not in available:
        return []
    current = connection.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
    disabled = {value for value in current.split(",") if value}
    disabled.add("type_pushdown")
    connection.execute("SET disabled_optimizers=" + sql_value(",".join(sorted(disabled))))
    return ["type_pushdown"]


def _verify_frozen(
    connection: Any,
    path: Path,
    raw_parameters: dict[str, Any],
    frozen_parameters: dict[str, Any],
    expected_rows: int,
    batch_size: int,
) -> int:
    """Exercise the actual CSV typed reader, not just SQL casts of strings."""
    names = list(raw_parameters["columns"])
    raw_query = "SELECT " + ", ".join(map(identifier, names))
    raw_query += " FROM " + read_expression(path, raw_parameters)
    typed_query = "SELECT " + ", ".join(f"CAST({identifier(name)} AS VARCHAR)" for name in names)
    typed_query += " FROM " + read_expression(path, frozen_parameters)
    verified = 0
    with connection.cursor() as raw_reader, connection.cursor() as typed_reader:
        # Older DuckDB cursor() connections do not inherit the parent's session
        # timezone. Set it on both instead of relying on version-specific cloning.
        raw_reader.execute("SET TimeZone='UTC'")
        typed_reader.execute("SET TimeZone='UTC'")
        _disable_type_pushdown(typed_reader)
        raw_reader.execute(raw_query)
        typed_reader.execute(typed_query)
        while True:
            raw_batch = raw_reader.fetchmany(batch_size)
            typed_batch = typed_reader.fetchmany(batch_size)
            if len(raw_batch) != len(typed_batch):
                raise AuditError("Frozen import returned a different number of data records")
            if not raw_batch:
                break
            for raw, typed in zip(raw_batch, typed_batch, strict=True):
                verified += 1
                if raw != typed:
                    index = next(
                        i for i, (a, b) in enumerate(zip(raw, typed, strict=True)) if a != b
                    )
                    raise AuditError(
                        f"Frozen import changed data row {verified}, column {names[index]!r}; "
                        "the engine's CSV and SQL conversion paths disagree"
                    )
    if verified != expected_rows:
        raise AuditError("The record count changed during frozen-import verification")
    return verified


def audit(
    source: str | Path,
    options: AuditOptions | None = None,
    *,
    events: TextIO | None = None,
) -> dict[str, Any]:
    """Audit every decoded field. Optional events are full, untruncated JSONL.

    data_row is the 1-based record returned by DuckDB after skip/header handling,
    not a physical line number. CSV quoting and line endings are outside the
    lexical cell contract.
    """
    import json

    options = options or AuditOptions()
    _validate_options(options)
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise AuditError(f"Not a local file: {path}")
    validate_encoding = None
    if options.encoding.lower().replace("_", "-") == "utf-16":
        with path.open("rb") as stream:
            magic = stream.read(4)
            if magic[:2] == b"\xfe\xff":
                raise AuditError(
                    "UTF-16BE input needs transcoding before this audit. DuckDB can misread "
                    "its byte order. Use iconv -f UTF-16 -t UTF-8 and audit the UTF-8 file."
                )
            if not (magic.startswith(b"\x1f\x8b") or magic == b"\x28\xb5\x2f\xfd"):
                validate_encoding = "utf-16"
    before = fingerprint(path, validate_encoding)
    if before["size_bytes"] == 0:
        raise AuditError("Cannot audit an empty file")
    warnings = []
    if options.header is None:
        warnings.append("Header presence was inferred; use --header yes/no to confirm it.")
    if options.skip:
        warnings.append(f"The first {options.skip} source lines were explicitly excluded.")
    if options.baseline_engine_version and options.baseline_engine_version != duckdb.__version__:
        warnings.append(
            f"Baseline used DuckDB {options.baseline_engine_version}; this audit uses "
            f"{duckdb.__version__}. The frozen import was verified again."
        )
    try:
        with duckdb.connect(
            config={
                "threads": 1,
                "memory_limit": f"{options.memory_limit_mb}MB",
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "preserve_insertion_order": True,
            }
        ) as connection:
            connection.execute("SET TimeZone='UTC'")
            disabled_optimizers = _disable_type_pushdown(connection)
            sniff = _sniff(connection, path, options)
            if sniff.get("RelaxedSniffRetry"):
                warnings.append(
                    "Strict sniffing rejected the supplied dialect; retried inference with a "
                    "relaxed sniffer. Data ingestion and frozen verification remained strict."
                )
            if sniff.get("InferenceUnavailable"):
                warnings.append(
                    "DuckDB's sniffer rejected this explicit UTF-16/CR dialect. Python csv read "
                    "the first record's width; unspecified candidate types defaulted to VARCHAR."
                )
            if sniff["SkipRows"] != options.skip:
                raise AuditError(
                    f"DuckDB proposed skipping {sniff['SkipRows']} lines. "
                    "Specify --skip explicitly after reviewing those lines."
                )
            columns = sniff["Columns"]
            if not columns:
                raise AuditError("DuckDB detected no columns")
            names = [column["name"] for column in columns]
            if options.expected_columns is not None and tuple(names) != options.expected_columns:
                raise AuditError(
                    "Column layout changed from the frozen configuration. "
                    "Audit without --read-config to inspect and accept the new layout."
                )
            unknown = set(options.types) - set(names)
            if unknown:
                raise AuditError(f"Unknown candidate column(s): {', '.join(sorted(unknown))}")
            candidates = {
                column["name"]: canonical_type(options.types.get(column["name"], column["type"]))
                for column in columns
            }
            parameters = _parameters(sniff, options)
            raw_parameters = {
                **parameters,
                "columns": dict.fromkeys(names, "VARCHAR"),
                "force_not_null": names,
            }
            expressions = []
            for name in names:
                raw = identifier(name)
                parsed = _cast(raw, candidates[name], parameters)
                if options.null_values:
                    parsed = (
                        f"CASE WHEN {raw} IN ({', '.join(map(sql_value, options.null_values))}) "
                        f"THEN NULL ELSE {parsed} END"
                    )
                expressions.extend([raw, f"CAST({parsed} AS VARCHAR)"])
            query = "SELECT " + ", ".join(expressions)
            query += " FROM " + read_expression(path, raw_parameters)
            reader = connection.execute(query)
            stats = {
                name: {
                    "name": name,
                    "column_index": index + 1,
                    "inferred_type": None
                    if sniff.get("InferenceUnavailable")
                    else columns[index]["type"],
                    "candidate_type": candidates[name],
                    "lossy_cells": 0,
                    "reasons": Counter(),
                }
                for index, name in enumerate(names)
            }
            examples = []
            row_count = loss_count = 0
            batch_size = min(options.batch_size, max(1, 16_384 // (2 * len(names))))
            while batch := reader.fetchmany(batch_size):
                for row in batch:
                    row_count += 1
                    for index, name in enumerate(names):
                        raw, rendered = row[2 * index : 2 * index + 2]
                        if raw == rendered:
                            continue
                        reason = _reason(raw, rendered, candidates[name], options.null_values)
                        loss_count += 1
                        stats[name]["lossy_cells"] += 1
                        stats[name]["reasons"][reason] += 1
                        event = {
                            "data_row": row_count,
                            "column": name,
                            "column_index": index + 1,
                            "candidate_type": candidates[name],
                            "reason": reason,
                            "raw": raw,
                            "rendered": rendered,
                        }
                        if events is not None:
                            events.write(json.dumps(event, ensure_ascii=False) + "\n")
                        if len(examples) < options.max_examples:
                            examples.append(
                                {
                                    **event,
                                    "raw": _example_value(raw, options.max_cell_chars),
                                    "rendered": _example_value(rendered, options.max_cell_chars),
                                }
                            )
            if row_count == 0:
                raise AuditError(
                    "No data records were read; cannot validate a header-only/empty CSV"
                )
            recommended = {}
            for name in names:
                column = stats[name]
                column["reasons"] = dict(column["reasons"])
                column["recommended_type"] = (
                    "VARCHAR" if column["lossy_cells"] else candidates[name]
                )
                recommended[name] = column["recommended_type"]
            frozen = {
                **parameters,
                "columns": recommended,
                "force_not_null": names,
                "allow_quoted_nulls": True,
            }
            if options.null_values:
                frozen["nullstr"] = list(options.null_values)
            verified_rows = _verify_frozen(
                connection, path, raw_parameters, frozen, row_count, batch_size
            )
            frozen_sql = (
                "SET TimeZone='UTC';\nSELECT * FROM " + read_expression(path, frozen) + ";\n"
            )
    except duckdb.Error as error:
        message = str(error)
        if len(message) > 5_000:
            message = message[:3_000] + "\n[DuckDB diagnostic truncated]\n" + message[-1_800:]
        hint = ""
        if options.encoding.lower().replace("_", "-") == "utf-16" and "utf-16" in str(error):
            hint = (
                "\nDuckDB's UTF-16 decoder can reject valid supplementary characters. "
                "Transcode to UTF-8 (e.g. iconv -f UTF-16 -t UTF-8) and audit that file."
            )
        raise AuditError(f"DuckDB could not complete the audit: {message}{hint}") from error
    after = fingerprint(path, validate_encoding)
    if before != after:
        raise AuditError("The source file changed during the audit; no result can be frozen")
    return {
        "format_version": 1,
        "engine": {
            "name": "duckdb",
            "version": duckdb.__version__,
            "timezone": "UTC",
            "audit_disabled_optimizers": disabled_optimizers,
        },
        "input": before,
        "contract": "decoded cell text == DuckDB canonical VARCHAR rendering after candidate parse",
        "data_row_definition": "1-based DuckDB data record after header/skip; not a physical line",
        "sniff_sample_size": options.sample_size,
        "type_inference": "unavailable_defaulted_to_text"
        if sniff.get("InferenceUnavailable")
        else "duckdb_sniff",
        "sniffed_newline": sniff["NewLineDelimiter"],
        "sniffed_date_format": sniff.get("DateFormat"),
        "sniffed_timestamp_format": sniff.get("TimestampFormat"),
        "candidate_null_values": list(options.null_values),
        "baseline": {
            "source_sha256": options.baseline_source_sha256,
            "engine_version": options.baseline_engine_version,
            "expected_columns": list(options.expected_columns),
        }
        if options.expected_columns is not None
        else None,
        "rows_audited": row_count,
        "cells_audited": row_count * len(names),
        "lossy_cells": loss_count,
        "lossless": loss_count == 0,
        "columns": list(stats.values()),
        "examples": examples,
        "examples_omitted": loss_count - len(examples),
        "warnings": warnings,
        "frozen": {
            "parameters": frozen,
            "sql": frozen_sql,
            "source_sha256": before["sha256"],
            "verified_rows": verified_rows,
            "verified": True,
            "config": {
                "format": "csv-rewind.duckdb",
                "version": 1,
                "timezone": "UTC",
                "engine_version": duckdb.__version__,
                "source_sha256": before["sha256"],
                "sniffed_newline": sniff["NewLineDelimiter"],
                "parameters": frozen,
            },
        },
    }
