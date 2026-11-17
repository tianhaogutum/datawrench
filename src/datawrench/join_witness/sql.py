"""Generate executable assertions; values and identifiers are always quoted."""

from __future__ import annotations

import csv
import os
from pathlib import Path

from tableproof.audit import read_header
from tableproof.models import JoinSpec, TableProofError


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _literal_glob_path(path: Path) -> str:
    # DuckDB expands globs even for a SQL string naming an existing literal file.
    # Escape each metacharacter with a one-character glob class, not SQL escapes.
    value = str(path.resolve())
    if os.name == "posix" and "\\" in value and any(char in value for char in "*?[]"):
        raise TableProofError("DuckDB SQL export cannot safely combine a literal backslash and glob characters in a POSIX path; copy the input to a simple filename")
    escapes = {"*": "[*]", "?": "[?]", "[": "[[]", "]": "[]]"}
    return "".join(escapes.get(char, char) for char in value)


def _record_newline(path: Path, delimiter: str) -> str:
    """Identify consistent record terminators with the standard CSV reader.

    DuckDB 2.0 can silently read zero rows when a CRLF file has an LF inside a
    quoted header and newline sniffing is disabled. Explicitly naming the record
    terminator prevents that false successful assertion.
    """
    last_line = ""
    seen = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            def lines():
                nonlocal last_line
                for line in stream:
                    last_line = line
                    yield line

            for _ in csv.reader(lines(), delimiter=delimiter, strict=True):
                ending = (r"\r\n" if last_line.endswith("\r\n") else r"\r" if last_line.endswith("\r")
                          else r"\n" if last_line.endswith("\n") else None)
                if ending is not None:
                    seen.add(ending)
                if len(seen) > 1:
                    raise TableProofError(f"Mixed record terminators cannot be exported safely to DuckDB SQL: {path}")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise TableProofError(f"Cannot inspect CSV record terminators in {path}: {exc}") from exc
    return next(iter(seen), r"\n")


def source_sql(path: Path, *, positional: bool = False, header: bool = True) -> str:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    fields = read_header(path)
    return _source_sql(path, fields, _record_newline(path, delimiter), positional=positional, header=header)


def _source_sql(path: Path, fields: tuple[str, ...], newline: str, *, positional: bool, header: bool) -> str:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    names = tuple(f"__jw_column_{index}" for index in range(len(fields))) if positional else fields
    columns = "{" + ", ".join(f"{literal(name)}: 'VARCHAR'" for name in names) + "}"
    return (
        f"read_csv({literal(_literal_glob_path(path))}, header={str(header).lower()}, auto_detect=false, "
        f"columns={columns}, delim={literal(delimiter)}, quote='\"', escape='\"', "
        f"new_line={literal(newline)}, "
        "nullstr='', allow_quoted_nulls=true, compression='none')"
    )


def _header_condition(header_source: str, fields: tuple[str, ...]) -> str:
    # Compare bytes: SQL identifier rules and a user's default collation must
    # not merge distinct source headers. Hex also safely represents NUL bytes.
    checks = " AND ".join(
        f"encode({identifier(f'__jw_column_{index}')}) = unhex({literal(name.encode('utf-8').hex())})"
        for index, name in enumerate(fields)
    )
    return f"coalesce((SELECT {checks} FROM {header_source} LIMIT 1), false)"


def assertions(spec: JoinSpec) -> list[dict[str, str]]:
    """Global uniqueness constraints on usable keys, in DuckDB SQL.

    Empty key components never participate in equality matching, as in TableProof.
    Null-key policy is a separate assertion so its failure is not mislabelled as
    a duplicate-key error. Unmatched keys are diagnosed by the upstream audit.
    """
    output = []
    unique_sides = {
        "one-to-one": {"left", "right"},
        "one-to-many": {"left"},
        "many-to-one": {"right"},
        "many-to-many": set(),
    }[spec.relationship]
    for side in ("left", "right"):
        if side not in unique_sides and spec.null_keys != "error":
            continue
        path = getattr(spec, side)
        keys = getattr(spec, side + "_keys")
        header = read_header(path)
        # DuckDB identifiers are case insensitive, even when quoted. Positional
        # names preserve TableProof's distinct `id` and `ID` source columns.
        try:
            key_names = [f"__jw_column_{header.index(key)}" for key in keys]
        except ValueError as exc:
            raise TableProofError(f"Input header changed while generating SQL: {path}") from exc
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        newline = _record_newline(path, delimiter)
        data_source = _source_sql(path, header, newline, positional=True, header=True)
        header_source = _source_sql(path, header, newline, positional=True, header=False)
        columns = ", ".join(f"encode({identifier(key)})" for key in key_names)
        guard = _header_condition(header_source, header)
        prefix = (f"SELECT CASE WHEN NOT ({guard}) THEN "
                  f"error({literal(side + ' input header changed; regenerate assertions')})\nWHEN NOT EXISTS (\n")
        usable = " AND ".join(
            f"({identifier(key)} IS NOT NULL AND {identifier(key)} <> '')" for key in key_names
        )
        if side in unique_sides:
            sql = (
                prefix +
                f"  SELECT {columns} FROM {data_source}\n"
                f"  WHERE {usable}\n"
                f"  GROUP BY {columns} HAVING COUNT(*) > 1\n"
                f") THEN true ELSE error({literal(side + ' usable join keys must be unique')}) END "
                f"AS {identifier(side + '_cardinality_ok')};"
            )
            output.append({"name": side + "_unique", "dialect": "duckdb", "sql": sql})
        if spec.null_keys == "error":
            sql = (
                prefix +
                f"  SELECT 1 FROM {data_source} WHERE NOT ({usable})\n"
                f") THEN true ELSE error({literal(side + ' join keys must be nonblank')}) END "
                f"AS {identifier(side + '_nonblank_ok')};"
            )
            output.append({"name": side + "_nonblank", "dialect": "duckdb", "sql": sql})
    return output


def sql_script(items: list[dict[str, str]]) -> str:
    header = (
        "-- Generated by datawrench join-witness for DuckDB. Read-only assertions.\n"
        "-- Exact string keys; empty components never match.\n"
        "-- These assert cardinality/null policy; see TableProof for coverage findings.\n"
    )
    if not items:
        return header + "SELECT true AS unrestricted_many_to_many;\n"
    return header + "\n\n".join(item["sql"] for item in items) + "\n"
