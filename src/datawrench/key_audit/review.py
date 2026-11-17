"""Validate the immutable review contract before selective export."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .audit import MAX_REVIEW_BYTES, SCHEMA, review_id
from .csvio import AuditError, CSVOptions
from .rules import engine_identity, validate_rules


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise AuditError(f"duplicate JSON member in review: {name!r}")
        result[name] = value
    return result


def reject_constant(value):
    raise AuditError(f"invalid JSON numeric constant: {value}")


def load_review(path: Path, *, require_engine: bool = True) -> dict:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise AuditError("review must be a regular file, not a pipe or device")
        content = source.read(MAX_REVIEW_BYTES + 1)
    if len(content) > MAX_REVIEW_BYTES:
        raise AuditError("review exceeds 32 MiB; regenerate with smaller detail limits")
    try:
        report = json.loads(
            content, object_pairs_hook=unique_object, parse_constant=reject_constant
        )
    except (ValueError, UnicodeError, RecursionError) as e:
        raise AuditError(f"invalid review JSON: {e}") from e
    try:
        if not isinstance(report, dict) or report["schema"] != SCHEMA:
            raise AuditError("unsupported review schema; regenerate with this version of datawrench key-audit")
        if require_engine and report["engine"] != engine_identity():
            raise AuditError(
                "review Unicode/Python/ftfy engine differs; regenerate and review again"
            )
        plan = report["plan"]
        columns = plan["key_columns"]
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) and c for c in columns)
        ):
            raise AuditError("review key_columns must be nonempty strings")
        if len(set(columns)) != len(columns):
            raise AuditError("review key_columns must not repeat")
        validate_rules(plan["rules"])
        CSVOptions(**plan["csv"])
        source = report["source"]
        if not isinstance(source["sha256"], str) or not re.fullmatch(
            r"[a-f0-9]{64}", source["sha256"]
        ):
            raise AuditError("invalid source SHA-256 in review")
        if type(source["bytes"]) is not int or source["bytes"] < 0:
            raise AuditError("invalid source byte count in review")
        if report["review_id"] != review_id(report):
            raise AuditError("review contract changed; regenerate and review again")
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        if isinstance(e, AuditError):
            raise
        raise AuditError(f"malformed review contract: {e}") from e
    return report


def confirmed_rules(report: dict, confirmed: list[str]) -> tuple[str, ...]:
    validate_rules(confirmed)
    proposed = report["plan"]["rules"]
    absent = [name for name in confirmed if name not in proposed]
    if absent:
        raise AuditError(f"confirmed rules were not in the review: {absent!r}")
    # Confirmation is a set selection; the original reviewed order is retained.
    return tuple(name for name in proposed if name in confirmed)
