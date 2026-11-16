"""Read-only artifact checks against a freshly computed local input plan."""
from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq

from .catalog import scan
from .data import OrderedRowSummary, RowSummary, Summary
from .types import UnifyError, is_list, pointer_path, schema_spec, type_spec


def _schema_issues(actual, target, file):
    issues = []

    def issue(parts, reason, expected, observed):
        issues.append({"file": str(file), "path": pointer_path(parts), "path_parts": list(parts),
                       "reason": reason, "expected": expected, "actual": observed})

    def fields(observed, expected, parts):
        an, tn = [f.name for f in observed], [f.name for f in expected]
        if len(set(an)) != len(an):
            issue(parts, "duplicate_fields", tn, an)
        if set(an) == set(tn) and an != tn:
            issue(parts, "field_order", tn, an)
        for f in expected:
            if f.name not in an:
                issue(parts + (f.name,), "missing_field", type_spec(f.type), None)
            elif an.count(f.name) == 1:
                types(observed.field(f.name).type, f.type, parts + (f.name,))
        for f in observed:
            if f.name not in tn:
                issue(parts + (f.name,), "unexpected_field", None, type_spec(f.type))

    def types(a, t, parts):
        if pa.types.is_struct(a) and pa.types.is_struct(t):
            fields(a, t, parts)
        elif is_list(a) and is_list(t):
            types(a.value_type, t.value_type, parts + (None,))
        elif ((a in (pa.string(), pa.large_string()) and t == pa.string())
              or (a in (pa.binary(), pa.large_binary()) and t == pa.binary())):
            return  # Offset widths are carriers; temporal units and zones must match exactly.
        elif a != t:
            issue(parts, "type", type_spec(t), type_spec(a))

    fields(actual, target, ())
    return issues


def _metric_unavailable(issues, columns):
    """Reuse exact schema evidence; root order is irrelevant to name-selected metrics."""
    order = {name: index for index, name in enumerate(columns)}
    unique = {}
    for issue in issues:
        parts = issue["path_parts"]
        if parts and parts[0] in order:
            record = {"column": parts[0], **issue}
            unique[json.dumps(record, sort_keys=True, ensure_ascii=True)] = record
    return sorted(unique.values(), key=lambda r: (
        order[r["column"]], r["file"], r["path"],
        json.dumps(r["path_parts"], ensure_ascii=True), r["reason"]))


def _group_key(record):
    return record["file"], record["row_group"]


def _column_metrics(catalog, target, null_columns, checksum_columns, row_columns, ordered_columns, batch_size, *,
                    source, invalid_by_file=None, ordered_group_evidence=False):
    """Collect all metrics in one pass; synthesize missing fields only for input.

    With ordered_group_evidence, ordered tuples are also digested per physical row
    group. Each group is read exactly once, in footer order, by the same bounded
    reader; the group-free path keeps whole-file batches unchanged.
    """
    counts = dict.fromkeys(null_columns, 0)
    counts_by_file = {name: [] for name in null_columns}
    summaries = {name: Summary(pa.schema([target.field(name)])) for name in checksum_columns}
    checksums_by_file = {name: [] for name in checksum_columns}
    row_summary = RowSummary(pa.schema([target.field(name) for name in row_columns])) if row_columns else None
    rows_by_file = []
    ordered_summary = OrderedRowSummary(pa.schema([target.field(name) for name in ordered_columns])) if ordered_columns else None
    ordered_by_file = []
    ordered_groups = []
    value_columns = list(dict.fromkeys([*checksum_columns, *row_columns, *ordered_columns]))
    unknown = set()
    # Keep existing traversal untouched unless an explicit sequence is requested.
    entries = sorted(catalog.entries, key=lambda e: str(e.path)) if ordered_columns else catalog.entries
    for entry in entries:
        physical_nulls = []
        file_counts = {}
        for name in null_columns:
            nulls = 0
            if name in entry.partitions:
                if entry.partitions[name] is None:
                    counts[name] += entry.rows
                    nulls = entry.rows
            elif name in entry.physical.names:
                physical_nulls.append(name)
            elif source:
                counts[name] += entry.rows  # Planned missing fields become typed nulls.
                nulls = entry.rows
            else:
                counts[name] = None  # Missing artifact fields must never be synthesized.
                nulls = None
            file_counts[name] = {"file": str(entry.path), "nulls": nulls}
            counts_by_file[name].append(file_counts[name])
        invalid = set() if source else invalid_by_file[str(entry.path)]
        if not source:
            # Footer evidence also drives diagnostics; never normalize away defects.
            unknown.update(invalid.intersection(value_columns))
        # File eligibility is independent: a bad file blocks the aggregate, not
        # healthy file evidence encountered later in the same shared pass.
        file_summaries = {name: Summary(summaries[name].schema)
                          for name in checksum_columns if name not in invalid}
        file_checksums = {}
        for name in checksum_columns:
            file_checksums[name] = {"file": str(entry.path), "checksum": None}
            checksums_by_file[name].append(file_checksums[name])
        file_row_summary = (RowSummary(row_summary.schema)
                            if row_summary is not None and not invalid.intersection(row_columns) else None)
        if row_columns:
            file_row_checksum = {"file": str(entry.path), "checksum": None}
            rows_by_file.append(file_row_checksum)
        file_ordered_summary = (OrderedRowSummary(ordered_summary.schema)
                                if ordered_summary is not None and not invalid.intersection(ordered_columns) else None)
        if ordered_columns:
            file_ordered_checksum = {"file": str(entry.path), "checksum": None}
            ordered_by_file.append(file_ordered_checksum)
        active_checksums = list(file_summaries)
        active_rows = row_summary is not None and not unknown.intersection(row_columns)
        active_ordered = ordered_summary is not None and not unknown.intersection(ordered_columns)
        needed = list(dict.fromkeys([*active_checksums, *(row_columns if file_row_summary is not None else []),
                                     *(ordered_columns if file_ordered_summary is not None else [])]))
        if not physical_nulls and not needed:
            if ordered_group_evidence and ordered_columns:
                # Footer-only skip: physical groups remain evidence, but no value digest exists.
                if entry.row_groups is None:
                    raise UnifyError(f"missing footer row-group layout: {entry.path}")
                ordered_groups.extend({"file": str(entry.path), "row_group": group["row_group"],
                                       "rows": group["rows"], "checksum": None} for group in entry.row_groups)
            continue
        decoded_rows = 0
        with pq.ParquetFile(entry.path) as reader:
            # Group evidence visits each physical group once; otherwise one whole-file pass.
            segments = range(reader.num_row_groups) if ordered_group_evidence else [None]
            for group in segments:
                # Unflagged reads keep the exact existing iter_batches call.
                selection = {} if group is None else {"row_groups": [group]}
                group_summary = (OrderedRowSummary(ordered_summary.schema)
                                 if group is not None and file_ordered_summary is not None else None)
                # All physical columns avoid prefix ambiguity between literal 'a.b' and a/b.
                for batch in reader.iter_batches(batch_size=batch_size, use_threads=False, **selection):
                    decoded_rows += batch.num_rows
                    for name in physical_nulls:
                        nulls = batch.column(batch.schema.get_field_index(name)).null_count
                        # A missing field elsewhere blocks the aggregate, not this file's evidence.
                        file_counts[name]["nulls"] += nulls
                        if counts[name] is not None:
                            counts[name] += nulls
                    arrays = {}
                    for name in needed:
                        if name in entry.partitions:
                            array = pa.array([entry.partitions[name]] * batch.num_rows, type=pa.string())
                        elif name in entry.physical.names:
                            array = batch.column(batch.schema.get_field_index(name))
                        else:
                            array = pa.nulls(batch.num_rows, type=target.field(name).type)
                        arrays[name] = array
                    # Summaries normalize to the selected planned types, including
                    # nested unions, explicit stringification and temporal precision.
                    for name in active_checksums:
                        table = pa.Table.from_arrays([arrays[name]], names=[name])
                        file_summaries[name].add(table)
                        if name not in unknown:
                            summaries[name].add(table)
                    if file_row_summary is not None:
                        table = pa.Table.from_arrays([arrays[name] for name in row_columns], names=row_columns)
                        file_row_summary.add(table)
                        if active_rows:
                            row_summary.add(table)
                    if file_ordered_summary is not None:
                        table = pa.Table.from_arrays([arrays[name] for name in ordered_columns], names=ordered_columns)
                        file_ordered_summary.add(table)
                        if group_summary is not None:
                            group_summary.add(table)
                        if active_ordered:
                            ordered_summary.add(table)
                if group is not None:
                    ordered_groups.append({"file": str(entry.path), "row_group": group,
                                           "rows": reader.metadata.row_group(group).num_rows,
                                           "checksum": None if group_summary is None else group_summary.digest()})
        if decoded_rows != entry.rows:
            raise UnifyError(f"decoded row count differs from scanned footer: {entry.path}")
        for name, summary in file_summaries.items():
            file_checksums[name]["checksum"] = summary.report()["checksums"][name]
        if file_row_summary is not None:
            file_row_checksum["checksum"] = file_row_summary.digest()
        if file_ordered_summary is not None:
            file_ordered_checksum["checksum"] = file_ordered_summary.digest()
    checksums = {name: None if name in unknown else summary.report()["checksums"][name]
                 for name, summary in summaries.items()}
    row_checksum = row_summary.digest() if row_summary is not None and not unknown.intersection(row_columns) else None
    ordered_checksum = ordered_summary.digest() if ordered_summary is not None and not unknown.intersection(ordered_columns) else None
    for records in [*counts_by_file.values(), *checksums_by_file.values(), rows_by_file, ordered_by_file]:
        records.sort(key=lambda r: r["file"])
    ordered_groups.sort(key=_group_key)
    return (counts, checksums, row_checksum, ordered_checksum, counts_by_file, checksums_by_file, rows_by_file,
            ordered_by_file, ordered_groups)


def validate_artifact(catalog, target, output, *, null_columns=(), checksum_columns=(),
                      row_checksum_columns=(), ordered_row_checksum_columns=(), batch_size=65536, max_rows=None,
                      row_group_checksum_evidence=False):
    """Check schema, footer rows, selected null counts and optional typed checksums.

    Only the two declared catalogs are opened. Batches bound rows, not bytes;
    wide/nested values and Parquet decoder buffers can still consume large memory.
    """
    if batch_size <= 0:
        raise UnifyError("batch-size must be positive")
    if max_rows is not None and max_rows < 0:
        raise UnifyError("max-rows must be nonnegative")
    columns = list(dict.fromkeys(null_columns))
    unknown = [name for name in columns if name not in target.names]
    if unknown:
        raise UnifyError(f"unknown top-level null columns: {unknown!r}")
    checksum_columns = list(dict.fromkeys(checksum_columns))
    unknown = [name for name in checksum_columns if name not in target.names]
    if unknown:
        raise UnifyError(f"unknown top-level checksum columns: {unknown!r}")
    row_columns = list(dict.fromkeys(row_checksum_columns))
    unknown = [name for name in row_columns if name not in target.names]
    if unknown:
        raise UnifyError(f"unknown top-level row checksum columns: {unknown!r}")
    ordered_columns = list(dict.fromkeys(ordered_row_checksum_columns))
    unknown = [name for name in ordered_columns if name not in target.names]
    if unknown:
        raise UnifyError(f"unknown top-level ordered row checksum columns: {unknown!r}")
    if row_group_checksum_evidence and not ordered_columns:
        raise UnifyError("row-group checksum evidence requires --ordered-row-checksum-column")
    # Output is physical/materialized Parquet: never infer Hive or skip bad files.
    artifact = scan(output, row_groups=row_group_checksum_evidence)
    if artifact.unreadable:
        raise UnifyError(f"output unreadable files: {artifact.unreadable}")
    expected_rows = sum(e.rows for e in catalog.entries)
    actual_rows = sum(e.rows for e in artifact.entries)
    if max_rows is not None:
        for side, rows, scoped in (("input", expected_rows, catalog), ("output", actual_rows, artifact)):
            if rows > max_rows:
                contributions = [{"file": str(e.path), "rows": e.rows}
                                 for e in sorted(scoped.entries, key=lambda e: str(e.path))]
                evidence = json.dumps(contributions, ensure_ascii=True, separators=(",", ":"))
                raise UnifyError(f"{side} rows {rows} exceed max-rows {max_rows}; no column data decoded; "
                                 f"rows_by_file={evidence}")
    issues = [issue for e in artifact.entries for issue in _schema_issues(e.physical, target, e.path)]
    invalid_by_file = {str(e.path): set() for e in artifact.entries}
    for issue in issues:
        if issue["path_parts"]:
            invalid_by_file[issue["file"]].add(issue["path_parts"][0])
    (expected_nulls, expected_checksums, expected_row_checksum, expected_ordered_checksum, expected_null_files,
     expected_checksum_files, expected_row_files, expected_ordered_files, expected_ordered_groups) = _column_metrics(
        catalog, target, columns, checksum_columns, row_columns, ordered_columns, batch_size, source=True,
        ordered_group_evidence=row_group_checksum_evidence)
    (actual_nulls, actual_checksums, actual_row_checksum, actual_ordered_checksum, actual_null_files,
     actual_checksum_files, actual_row_files, actual_ordered_files, actual_ordered_groups) = _column_metrics(
        artifact, target, columns, checksum_columns, row_columns, ordered_columns, batch_size,
        source=False, invalid_by_file=invalid_by_file, ordered_group_evidence=row_group_checksum_evidence)
    # Parent validity remains measurable despite type/child/order defects. Only
    # a missing top-level artifact field makes its null count unavailable.
    null_unavailable = {name: [] for name in columns}
    missing_parents = [issue for issue in issues
                       if issue["reason"] == "missing_field" and len(issue["path_parts"]) == 1]
    for reason in _metric_unavailable(missing_parents, columns):
        null_unavailable[reason["column"]].append(reason)
    column_checks = {name: {"ok": actual_nulls[name] == expected_nulls[name],
                            "expected": expected_nulls[name], "actual": actual_nulls[name],
                            "unavailable": null_unavailable[name],
                            "expected_by_file": expected_null_files[name],
                            "actual_by_file": actual_null_files[name]} for name in columns}
    checks = {"schema": {"ok": not issues, "issues": issues},
              "rows": {"ok": expected_rows == actual_rows, "expected": expected_rows, "actual": actual_rows,
                       "expected_by_file": [{"file": str(e.path), "rows": e.rows}
                                            for e in sorted(catalog.entries, key=lambda e: str(e.path))],
                       "actual_by_file": [{"file": str(e.path), "rows": e.rows}
                                          for e in sorted(artifact.entries, key=lambda e: str(e.path))]},
              "null_counts": {"ok": all(c["ok"] for c in column_checks.values()), "columns": column_checks}}
    if checksum_columns:
        unavailable = {name: [] for name in checksum_columns}
        for reason in _metric_unavailable(issues, checksum_columns):
            unavailable[reason["column"]].append(reason)
        checksum_checks = {name: {"ok": actual_checksums[name] == expected_checksums[name],
                                  "expected": expected_checksums[name], "actual": actual_checksums[name],
                                  "unavailable": unavailable[name],
                                  "expected_by_file": expected_checksum_files[name],
                                  "actual_by_file": actual_checksum_files[name]}
                           for name in checksum_columns}
        checks["checksums"] = {"ok": all(c["ok"] for c in checksum_checks.values()), "columns": checksum_checks}
    if row_columns:
        checks["row_checksum"] = {"ok": actual_row_checksum == expected_row_checksum,
                                  "expected": expected_row_checksum, "actual": actual_row_checksum,
                                  "unavailable": _metric_unavailable(issues, row_columns),
                                  "expected_by_file": expected_row_files,
                                  "actual_by_file": actual_row_files}
    if ordered_columns:
        checks["ordered_row_checksum"] = {"ok": actual_ordered_checksum == expected_ordered_checksum,
                                          "expected": expected_ordered_checksum, "actual": actual_ordered_checksum,
                                          "unavailable": _metric_unavailable(issues, ordered_columns),
                                          "expected_by_file": expected_ordered_files,
                                          "actual_by_file": actual_ordered_files}
        if row_group_checksum_evidence:
            checks["ordered_row_checksum"]["expected_by_row_group"] = expected_ordered_groups
            checks["ordered_row_checksum"]["actual_by_row_group"] = actual_ordered_groups
    report = {"ok": all(c["ok"] for c in checks.values()),
            "input": {"root": str(catalog.root), "hive": catalog.hive,
                      "files": [str(e.path) for e in catalog.entries], "unreadable": catalog.unreadable},
            "output": {"root": str(artifact.root), "files": [str(e.path) for e in artifact.entries]},
            "expected": {"schema": schema_spec(target), "rows": expected_rows, "null_counts": expected_nulls},
            "actual": {"schema_by_file": [{"file": str(e.path), "schema": schema_spec(e.physical)}
                                          for e in artifact.entries],
                       "rows": actual_rows, "null_counts": actual_nulls},
            "checks": checks,
            "scope": {"values_checked": False, "null_columns": columns,
                      "null_semantics": "top-level parent validity only; no nested child/element null counts",
                      "rows_from": "Parquet footers", "batch_size": batch_size, "max_rows_per_side": max_rows,
                      "memory": "one decoded batch, all physical columns; row bound only, no byte/RSS cap",
                      "schema_ignored": ["metadata", "nullability", "list element names", "offset widths"]}}
    if checksum_columns:
        report["expected"]["checksums"] = expected_checksums
        report["actual"]["checksums"] = actual_checksums
        report["scope"].update({
            "values_checked": True, "checksum_columns": checksum_columns,
            "checksum_semantics": "selected planned-type per-column multisets; duplicates/nulls/nested values included; "
                                  "NaNs and signed zero normalized; no row order/association or unselected value checks",
        })
    if row_columns:
        report["expected"]["row_checksum"] = expected_row_checksum
        report["actual"]["row_checksum"] = actual_row_checksum
        report["scope"].update({
            "values_checked": True, "row_checksum_columns": row_columns,
            "row_checksum_semantics": "selected ordered planned-type tuple multiset; duplicates/nulls/nested values included; "
                                      "NaNs and signed zero normalized; no row order or unselected value checks",
        })
    if ordered_columns:
        report["expected"]["ordered_row_checksum"] = expected_ordered_checksum
        report["actual"]["ordered_row_checksum"] = actual_ordered_checksum
        report["scope"].update({
            "values_checked": True, "ordered_row_checksum_columns": ordered_columns,
            "ordered_row_checksum_semantics": "selected ordered planned-type tuple sequence; duplicates/nulls/nested values included; "
                                              "NaNs and signed zero normalized; no unselected value checks",
            "ordered_row_checksum_order": "each side independently: sorted absolute file-path strings, then row order within each file; "
                                          "caller-chosen logical order, not a Parquet dataset ordering guarantee",
        })
    return report
