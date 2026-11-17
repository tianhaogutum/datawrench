"""Explain multiplicity without materializing the wide join.

TableProof owns the table validation, counts, predictions, and policy verdict.
This module adds bounded witnesses and a streaming non-key metric pass.
"""

from __future__ import annotations

import csv
import hashlib
import heapq
import json
import re
import stat
from dataclasses import dataclass, replace
from decimal import Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

import tableproof
from tableproof import JoinSpec, TableProofError, audit_join
from tableproof.audit import read_table
from tableproof.models import Key, TableData

from .sql import assertions

NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


@dataclass
class _SampleBudget:
    limit: int
    used: int = 0
    omitted_records: int = 0

    def attach_values(self, sample: dict[str, Any], header: list[str], row: list[str]) -> None:
        # Count compact UTF-8 JSON a field at a time. A very wide row never
        # needs a second full serialized copy merely to discover it won't fit.
        cost = 2  # object braces
        remaining = self.limit - self.used
        for index, (name, value) in enumerate(zip(header, row)):
            if cost > remaining:
                break
            cost += int(index > 0) + 1  # comma and colon
            cost += len(json.dumps(name, ensure_ascii=False).encode("utf-8"))
            cost += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if cost > remaining:
            self.omitted_records += 1
            sample["values_omitted"] = "sample_byte_limit"
        else:
            self.used += cost
            sample["values"] = dict(zip(header, row))


def _fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    try:
        str(path).encode("utf-8")
        info = path.stat()
    except UnicodeError as exc:
        raise TableProofError("Input paths must be valid UTF-8") from exc
    except ValueError as exc:
        raise TableProofError(f"Invalid input path: {path!r}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise TableProofError(f"Input must be a regular file: {path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _key_id(key: Key) -> str:
    # JSON arrays avoid ambiguous delimiters in composite-key components.
    payload = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _number(value: str, path: Path, record: int, column: str) -> Decimal | None:
    if value == "":
        return None
    if len(value) > 1100 or not NUMBER.fullmatch(value):
        raise TableProofError(f"Invalid numeric measure {column!r} in {path}, record {record}: {value[:80]!r}")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise TableProofError(f"Invalid numeric measure {column!r} in {path}, record {record}") from exc
    if not number.is_finite() or len(number.as_tuple().digits) > 1000 or abs(number.as_tuple().exponent) > 1000:
        raise TableProofError(f"Numeric measure outside supported 1000-digit/exponent range: {path}, record {record}, {column!r}")
    return number


def _text(number: Decimal) -> str:
    return "0" if number == 0 else str(number)


def _metric_summary() -> dict[str, Any]:
    return {"source_sum": Decimal(0), "retained_once_sum": Decimal(0), "predicted_join_sum": Decimal(0),
            "source_nonnull_rows": 0, "predicted_nonnull_rows": 0}


def _scan_records(table: TableData, opposite: TableData, side: str, how: str,
                  metrics: tuple[str, ...], selected: dict[Key, dict[str, Any]],
                  sample_rows: int, redact: bool, budget: _SampleBudget) -> dict[str, Any]:
    missing = [column for column in metrics if column not in table.header]
    if missing:
        raise TableProofError(f"Missing measure columns {missing} in {table.path}")
    if len(set(metrics)) != len(metrics):
        raise TableProofError(f"Repeated {side} measure columns")
    totals = {column: _metric_summary() for column in metrics}
    for group in selected.values():
        group[side + "_measures"] = {column: _metric_summary() for column in metrics}
    remaining_samples = sum(min(sample_rows, group[side + "_count"]) for group in selected.values())
    if not metrics and remaining_samples == 0:
        return totals
    preserved = how == "full" or how == side
    delimiter = "\t" if table.delimiter == "tab" else ","
    with localcontext(Context(prec=4096, Emin=-999999, Emax=999999)) as context:
        # Numeric inputs are capped at 1000 digits and absolute exponent 1000.
        # This leaves >1000 digits of headroom for sums and copy multiplicities.
        context.prec = 4096
        try:
            with table.path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream, delimiter=delimiter, strict=True)
                header = next(reader)
                key_indexes = [header.index(column) for column in table.key_columns]
                metric_indexes = [(column, header.index(column)) for column in metrics]
                previous_line = reader.line_num
                for record, row in enumerate(reader, start=1):
                    line_start, previous_line = previous_line + 1, reader.line_num
                    if len(row) != len(header):
                        raise TableProofError(f"Input changed or malformed row in {table.path}, record {record}")
                    key = tuple(row[index] for index in key_indexes)
                    matches = opposite.keys.get(key, 0) if all(key) else 0
                    multiplicity = matches or int(preserved)
                    group = selected.get(key)
                    if group is not None:
                        samples = group[side + "_records"]
                        if len(samples) < sample_rows:
                            sample = {"record_number": record, "line_start": line_start,
                                      "line_end": reader.line_num, "join_copies": multiplicity}
                            if not redact:
                                budget.attach_values(sample, header, row)
                            samples.append(sample)
                            remaining_samples -= 1
                    for column, index in metric_indexes:
                        number = _number(row[index], table.path, record, column)
                        if number is None:
                            continue
                        total = totals[column]
                        total["source_sum"] += number
                        total["source_nonnull_rows"] += 1
                        total["retained_once_sum"] += number if multiplicity else 0
                        total["predicted_join_sum"] += number * multiplicity
                        total["predicted_nonnull_rows"] += multiplicity
                        if group is not None:
                            values = group[side + "_measures"].setdefault(column, _metric_summary())
                            values["source_sum"] += number
                            values["source_nonnull_rows"] += 1
                            values["retained_once_sum"] += number if multiplicity else 0
                            values["predicted_join_sum"] += number * multiplicity
                            values["predicted_nonnull_rows"] += multiplicity
                    if not metrics and remaining_samples == 0:
                        # All original-record samples are complete. Upstream
                        # validated the entire file, and the final hash/stat
                        # check still verifies the snapshot after this pass.
                        break
        except (OSError, UnicodeError, csv.Error, ValueError, StopIteration) as exc:
            raise TableProofError(f"Cannot scan original records in {table.path}: {exc}") from exc
        for collection in [totals, *(group[side + "_measures"] for group in selected.values())]:
            for column, values in collection.items():
                values["added_by_repetition"] = values["predicted_join_sum"] - values["retained_once_sum"]
                values["dropped_sum"] = values["source_sum"] - values["retained_once_sum"]
                for name, value in list(values.items()):
                    if isinstance(value, Decimal):
                        values[name] = _text(value)
    return totals


def _rank_measure(table: TableData, opposite: TableData, column: str) -> dict[Key, Decimal]:
    """Aggregate only the requested ranking measure at keys that repeat it."""
    if column not in table.header:
        raise TableProofError(f"Missing ranking measure column {column!r} in {table.path}")
    scores: dict[Key, Decimal] = {}
    with localcontext(Context(prec=4096, Emin=-999999, Emax=999999)) as context:
        context.prec = 4096
        try:
            with table.path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream, delimiter="\t" if table.delimiter == "tab" else ",", strict=True)
                header = next(reader)
                if tuple(header) != table.header:
                    raise TableProofError(f"Input header changed during ranking: {table.path}")
                key_indexes = [header.index(key) for key in table.key_columns]
                index = header.index(column)
                for record, row in enumerate(reader, start=1):
                    if len(row) != len(header):
                        raise TableProofError(f"Input changed or malformed row in {table.path}, record {record}")
                    key = tuple(row[position] for position in key_indexes)
                    copies = opposite.keys.get(key, 0) if all(key) else 0
                    if copies > 1:
                        number = _number(row[index], table.path, record, column)
                        if number is not None:
                            scores[key] = scores.get(key, Decimal(0)) + number * (copies - 1)
        except (OSError, UnicodeError, csv.Error, ValueError, StopIteration) as exc:
            raise TableProofError(f"Cannot rank original measures in {table.path}: {exc}") from exc
    return scores


def analyze(spec: JoinSpec, *, how: str | None = None, left_measures: tuple[str, ...] = (),
            right_measures: tuple[str, ...] = (), top: int = 10, sample_rows: int = 3,
            redact: bool = False, sample_bytes: int = 4 * 1024 * 1024,
            rank_by: str = "rows") -> dict[str, Any]:
    """Return a TableProof audit with ranked, bounded original-record witnesses.

    The relationship constrains keys across each entire input, like TableProof's
    validator. Contributions describe matched key groups only. A duplicate
    unmatched key may fail the contract without adding joined copies.
    """
    if not isinstance(rank_by, str):
        raise TableProofError("rank_by must be rows or left:COLUMN or right:COLUMN")
    rank_side, rank_column = None, None
    if rank_by != "rows":
        rank_side, separator, rank_column = rank_by.partition(":")
        if not separator or rank_side not in {"left", "right"} or not rank_column:
            raise TableProofError("rank_by must be rows or left:COLUMN or right:COLUMN")
        if rank_side == "left" and rank_column not in left_measures:
            left_measures = (*left_measures, rank_column)
        elif rank_side == "right" and rank_column not in right_measures:
            right_measures = (*right_measures, rank_column)
    how = (spec.join_type or "left") if how is None else how
    if not isinstance(how, str) or how not in {"inner", "left", "right", "full"}:
        raise TableProofError(f"Unsupported join type: {how}")
    if spec.result is not None and spec.join_type != how:
        raise TableProofError("Cannot override the join type of a materialized result contract")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (top, sample_rows, sample_bytes)):
        raise TableProofError("top, sample_rows, and sample_bytes must be integers")
    if not isinstance(redact, bool):
        raise TableProofError("redact must be a boolean")
    if not 0 <= top <= 100 or not 0 <= sample_rows <= 20:
        raise TableProofError("top must be 0..100 and sample_rows must be 0..20")
    if not 0 <= sample_bytes <= 16 * 1024 * 1024:
        raise TableProofError("sample_bytes must be 0..16777216")
    if len(set(spec.left_keys)) != len(spec.left_keys) or len(set(spec.right_keys)) != len(spec.right_keys):
        raise TableProofError("Join key columns must not repeat")
    spec = replace(spec, join_type=how, show_raw_keys=not redact)
    try:
        paths = [spec.left, spec.right] + ([spec.result] if spec.result is not None else [])
        before = {path: _fingerprint(path) for path in paths}
        upstream = audit_join(spec)
        expected_hashes = {spec.left: upstream["left"]["sha256"], spec.right: upstream["right"]["sha256"]}
        if spec.result is not None:
            expected_hashes[spec.result] = upstream["result"]["sha256"]
        left = read_table(spec.left, spec.left_keys)
        right = read_table(spec.right, spec.right_keys)
        for side, table in (("left", left), ("right", right)):
            if table.sha256 != upstream[side]["sha256"]:
                raise TableProofError(f"Input changed during audit: {table.path}")
        summary = {"predicted_join_rows": upstream["predictions"][how]["rows"],
                   "matched_key_groups": 0, "contributing_key_groups": 0,
                   "contract_violating_matched_key_groups": 0,
                   "contract_excess_matched_rows": 0,
                   "left_repeated_source_rows": 0, "right_repeated_source_rows": 0,
                   "left_added_row_copies": 0, "right_added_row_copies": 0}
        cap_left = spec.relationship in {"one-to-one", "one-to-many"}
        cap_right = spec.relationship in {"one-to-one", "many-to-one"}
        rank_scores = (_rank_measure(left, right, rank_column) if top and rank_side == "left"
                       else _rank_measure(right, left, rank_column) if top and rank_side == "right" else {})

        def contributions():
            for key, l_count in left.keys.items():
                r_count = right.keys.get(key, 0)
                if not all(key) or not r_count:
                    continue
                product = l_count * r_count
                excess = product - (1 if cap_left else l_count) * (1 if cap_right else r_count)
                summary["matched_key_groups"] += 1
                summary["contract_excess_matched_rows"] += excess
                summary["contract_violating_matched_key_groups"] += int(excess > 0)
                summary["left_repeated_source_rows"] += l_count if r_count > 1 else 0
                summary["right_repeated_source_rows"] += r_count if l_count > 1 else 0
                summary["left_added_row_copies"] += l_count * (r_count - 1)
                summary["right_added_row_copies"] += r_count * (l_count - 1)
                if l_count == r_count == 1:
                    continue
                summary["contributing_key_groups"] += 1
                yield (key, l_count, r_count, product, excess)

        def order(item):
            count_order = (-item[4], -max(item[1] * (item[2]-1), item[2] * (item[1]-1)), -item[3], item[0])
            if rank_side is None:
                return count_order
            # abs() and unary minus apply the caller's Decimal context and can
            # round long values. These copy operations preserve every digit.
            score = rank_scores.get(item[0], Decimal(0)).copy_abs().copy_negate()
            return (score, *count_order)

        # nsmallest keeps O(top) witnesses; optional metric ranking stores one
        # Decimal per key that repeats the selected measure, never a wide join.
        ranked = heapq.nsmallest(top, contributions(), key=order) if top else []
        if not top:
            for _ in contributions():
                pass
        selected = {}
        for key, l_count, r_count, product, excess in ranked:
            group = {"key_id": _key_id(key), "left_count": l_count, "right_count": r_count,
                     "joined_rows": product, "contract_excess_rows": excess,
                     "violates_relationship": excess > 0,
                     "left_added_row_copies": l_count * (r_count - 1),
                     "right_added_row_copies": r_count * (l_count - 1),
                     "left_records": [], "right_records": [],
                     "left_measures": {}, "right_measures": {}}
            if not redact:
                group["key"] = list(key)
            if rank_side is not None:
                group["ranking_measure_added"] = _text(rank_scores.get(key, Decimal(0)))
            selected[key] = group
        budget = _SampleBudget(sample_bytes)
        left_metrics = _scan_records(left, right, "left", how, left_measures, selected, sample_rows, redact, budget)
        right_metrics = _scan_records(right, left, "right", how, right_measures, selected, sample_rows, redact, budget)
        sql_assertions = assertions(spec)
        for path, fingerprint in before.items():
            if (_fingerprint(path) != fingerprint or _hash(path) != expected_hashes[path]
                    or _fingerprint(path) != fingerprint):
                raise TableProofError(f"Input changed during audit: {path}; retry with stable snapshots")
    except OSError as exc:
        raise TableProofError(str(exc)) from exc
    summary["reported_key_groups"] = len(selected)
    summary["omitted_key_groups"] = summary["contributing_key_groups"] - len(selected)
    for group in selected.values():
        for side in ("left", "right"):
            group[side + "_records_omitted"] = group[side + "_count"] - len(group[side + "_records"])
    return {
        "schema_version": "1.0", "tool": {"name": "join-witness", "version": "0.1.0",
                                             "upstream": "tableproof", "upstream_version": tableproof.__version__},
        "join_type": how, "expected_relationship": spec.relationship,
        "ranking": {"by": rank_by, "order": "absolute_added_by_repetition" if rank_side is not None else "contract_excess_then_directional_copies"},
        "verdict": upstream["verdict"], "redacted": redact,
        "summary": summary, "top_keys": list(selected.values()),
        "sampling": {"top_limit": top, "records_per_key_per_side": sample_rows,
                     "values_byte_limit": sample_bytes, "values_bytes": budget.used,
                     "records_without_values": budget.omitted_records},
        "measures": {"left": left_metrics, "right": right_metrics},
        "assertions": sql_assertions, "upstream_audit": upstream,
    }
