"""``datawrench key-audit``: command-line review, readable evidence, and confirmed-rule export."""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

from datawrench import __version__
from .audit import MAX_REVIEW_BYTES, Audit, DetailLimits
from .csvio import AuditError, CSVOptions, LimitedWriter, new_atomic_file, require_new_output
from .review import confirmed_rules, load_review
from .rules import RULES


def positive(value):
    try:
        n = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("expected a positive integer") from e
    if n < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return n


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="datawrench key-audit", description="Review new CSV key collisions before Unicode cleanup."
    )
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("rules", help="list the finite explicit cleanup rules")
    audit = sub.add_parser("audit", help="preview rules; write a source-bound JSON review")
    audit.add_argument("input", type=Path)
    audit.add_argument(
        "--key", action="append", required=True, help="exact key column; repeat for composite keys"
    )
    audit.add_argument(
        "--rule",
        action="append",
        required=True,
        choices=RULES,
        help="repeat in desired order; no default rules",
    )
    audit.add_argument(
        "--report",
        type=Path,
        required=True,
        help="new JSON file; existing paths are never overwritten",
    )
    audit.add_argument(
        "--delimiter", default=",", help="one character; use TAB for a tab delimiter"
    )
    audit.add_argument(
        "--encoding",
        default="utf-8-sig",
        choices=("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be", "cp1252"),
    )
    audit.add_argument("--quotechar", default='"')
    audit.add_argument("--escapechar")
    audit.add_argument("--no-doublequote", action="store_true")
    audit.add_argument("--max-field-chars", type=positive, default=1_048_576)
    audit.add_argument("--max-record-chars", type=positive, default=8_388_608)
    audit.add_argument("--max-key-chars", type=positive, default=4096)
    audit.add_argument(
        "--groups", type=positive, default=100, help="maximum example groups/changed keys (1..1000)"
    )
    audit.add_argument(
        "--variants",
        type=positive,
        default=20,
        help="maximum example variants per collision group (1..1000)",
    )
    audit.add_argument(
        "--samples",
        type=positive,
        default=5,
        help="maximum row references per original key (1..1000)",
    )
    audit.add_argument(
        "--work-dir",
        type=Path,
        help="existing directory for disposable source snapshot and SQLite index",
    )
    clean = sub.add_parser(
        "clean", help="re-audit and apply only explicitly confirmed reviewed rules"
    )
    clean.add_argument("input", type=Path)
    clean.add_argument("--review", type=Path, required=True)
    clean.add_argument(
        "--confirm",
        action="append",
        required=True,
        choices=RULES,
        help="select a reviewed rule; reviewed order is retained",
    )
    clean.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new CSV file; never overwrites input or existing output",
    )
    clean.add_argument("--output-encoding", choices=("utf-8", "utf-8-sig"), default="utf-8")
    clean.add_argument(
        "--accept-collisions",
        action="store_true",
        help="explicitly allow newly merged keys; keeps all rows",
    )
    clean.add_argument(
        "--accept-empty",
        action="store_true",
        help="explicitly allow nonempty key components to become empty",
    )
    clean.add_argument("--work-dir", type=Path)
    show = sub.add_parser("show", help="display bounded escaped evidence from a JSON review")
    show.add_argument("review", type=Path)
    mapping = sub.add_parser(
        "export-map", help="stream full key mappings for Miller/csvkit or complete review"
    )
    mapping.add_argument("input", type=Path)
    mapping.add_argument("--review", type=Path, required=True)
    mapping.add_argument(
        "--output", type=Path, required=True, help="new comma-delimited UTF-8 mapping CSV"
    )
    mapping.add_argument("--scope", choices=("collisions", "changed", "all"), default="collisions")
    mapping.add_argument("--work-dir", type=Path)
    return root


def safe_print(*parts, **kwargs):
    """Even a hand-edited review must not execute terminal control sequences."""
    escaped = [
        "".join(
            c if c == "\n" or c.isprintable() else c.encode("unicode_escape").decode("ascii")
            for c in str(part)
        )
        for part in parts
    ]
    print(*escaped, **kwargs)


def print_summary(summary: dict):
    safe_print(
        f"Rows: {summary['rows']}; original distinct keys: {summary['original_distinct_keys']}; "
        f"normalized distinct keys: {summary['normalized_distinct_keys']}"
    )
    safe_print(
        f"Existing duplicate groups: {summary['baseline_duplicate_groups']} "
        f"({summary['baseline_duplicate_rows']} rows)"
    )
    safe_print(
        f"New collision groups: {summary['new_collision_groups']} "
        f"({summary['new_collision_rows']} rows); newly empty key rows: {summary['newly_empty_key_rows']}"
    )


def display_changes(changes):
    for change in changes:
        if "diff_omitted" in change:
            safe_print(f"    {ascii(change['column'])}: {change['diff_omitted']}")
        for edit in change.get("edits", []):
            old = ", ".join(f"{p['codepoint']} {p['name']}" for p in edit["removed"]) or "<empty>"
            new = ", ".join(f"{p['codepoint']} {p['name']}" for p in edit["inserted"]) or "<empty>"
            safe_print(f"    {ascii(change['column'])}: {old} -> {new}")
            omitted = edit["omitted_removed_codepoints"] + edit["omitted_inserted_codepoints"]
            if omitted:
                safe_print(f"      ... {omitted} more changed code points")
        if change.get("omitted_edit_operations"):
            safe_print(f"    ... {change['omitted_edit_operations']} more edit operations")


def escaped_key(parts):
    return [
        part["escaped"]
        + (
            f"... (+{part['omitted_characters']} code points)"
            if part.get("omitted_characters")
            else ""
        )
        for part in parts
    ]


def display(report: dict):
    print_summary(report["summary"])
    safe_print("Rule order: " + ", ".join(report["plan"]["rules"]))
    for stage in report["stages"][1:]:
        safe_print(
            f"  {stage['rule']}: {stage['changed_rows']} changed rows, "
            f"{stage['additional_keys_merged']} additional original keys merged"
        )
    for variant in report["baseline_duplicates"]["groups"]:
        key = escaped_key(variant["original_key"])
        safe_print(
            f"\nExisting duplicate {json.dumps(key, ensure_ascii=True)}: {variant['row_count']} rows; samples {variant['row_samples']}"
        )
    if report["baseline_duplicates"]["omitted_groups"]:
        safe_print(
            f"... {report['baseline_duplicates']['omitted_groups']} more existing duplicate groups"
        )
    displayed_changes = set()
    for group in report["new_collisions"]["groups"]:
        key = escaped_key(group["normalized_key"])
        safe_print(
            f"\nCollision into {json.dumps(key, ensure_ascii=True)} "
            f"({group['row_count']} rows, {group['variant_count']} original keys)"
        )
        for variant in group["variants"]:
            original = escaped_key(variant["original_key"])
            safe_print(
                f"  {json.dumps(original, ensure_ascii=True)}: {variant['row_count']} rows; "
                f"samples {variant['row_samples']}"
            )
            display_changes(variant["changes"])
            if variant.get("original_key_id") is not None:
                displayed_changes.add(variant["original_key_id"])
        if group["omitted_variants"]:
            safe_print(f"  ... {group['omitted_variants']} more original keys (see detail limits)")
    if report["new_collisions"]["omitted_groups"]:
        safe_print(
            f"... {report['new_collisions']['omitted_groups']} more collision groups (see detail limits)"
        )
    for item in report["changed_keys"]["keys"]:
        if item.get("original_key_id") is not None and item["original_key_id"] in displayed_changes:
            continue
        before = escaped_key(item["original_key"])
        after = escaped_key(item["normalized_key"])
        safe_print(
            f"\nChanged {json.dumps(before, ensure_ascii=True)} -> {json.dumps(after, ensure_ascii=True)}; samples {item['row_samples']}"
        )
        display_changes(item["changes"])
    if report["changed_keys"]["omitted_keys"]:
        safe_print(f"... {report['changed_keys']['omitted_keys']} more changed original keys")
    for item in report.get("newly_empty_keys", {}).get("keys", []):
        key = escaped_key(item["original_key"])
        safe_print(
            f"\nNewly empty key component in {json.dumps(key, ensure_ascii=True)}; samples {item['row_samples']}"
        )
    if report.get("newly_empty_keys", {}).get("omitted_keys"):
        safe_print(
            f"... {report['newly_empty_keys']['omitted_keys']} more original keys with newly empty components"
        )
    if report.get("evidence_budget", {}).get("exhausted"):
        safe_print(
            "Report evidence budget reached; export-map provides complete full-length key mappings."
        )


def run(args) -> int:
    if args.command == "rules":
        for name, rule in RULES.items():
            print(f"{name:22} {rule.description}")
        return 0
    if args.command == "show":
        display(load_review(args.review, require_engine=False))
        return 0
    if args.command == "audit":
        require_new_output(args.report)
        delimiter = "\t" if args.delimiter == "TAB" else args.delimiter
        options = CSVOptions(
            delimiter=delimiter,
            encoding=args.encoding,
            quotechar=args.quotechar,
            escapechar=args.escapechar,
            doublequote=not args.no_doublequote,
            max_field_chars=args.max_field_chars,
            max_record_chars=args.max_record_chars,
            max_key_chars=args.max_key_chars,
        )
        limits = DetailLimits(args.groups, args.variants, args.samples)
        with Audit(args.input, args.key, args.rule, options, limits, args.work_dir) as audit:
            report = audit.report()
            with new_atomic_file(args.report) as (target, _):
                bounded = LimitedWriter(target, MAX_REVIEW_BYTES)
                json.dump(report, bounded, indent=2, ensure_ascii=True, allow_nan=False)
                bounded.write("\n")
            print_summary(report["summary"])
            print(f"Review: {str(args.report)!r}")
            return int(
                bool(
                    report["summary"]["new_collision_groups"]
                    or report["summary"]["newly_empty_key_rows"]
                )
            )
    if args.command in ("clean", "export-map"):
        require_new_output(args.output)
        report = load_review(args.review)
        selected = (
            confirmed_rules(report, args.confirm)
            if args.command == "clean"
            else report["plan"]["rules"]
        )
        plan = report["plan"]
        with Audit(
            args.input,
            plan["key_columns"],
            selected,
            CSVOptions(**plan["csv"]),
            work_dir=args.work_dir,
            expected_source=report["source"],
        ) as audit:
            if args.command == "export-map":
                receipt = audit.export_map(args.output, scope=args.scope)
                receipt["review_id"] = report["review_id"]
                print(json.dumps(receipt, indent=2, ensure_ascii=True))
                return 0
            summary = audit.summary()
            blocked = []
            if summary["new_collision_groups"] and not args.accept_collisions:
                blocked.append(
                    f"{summary['new_collision_groups']} new collision group(s); use --accept-collisions after review"
                )
            if summary["newly_empty_key_rows"] and not args.accept_empty:
                blocked.append(
                    f"{summary['newly_empty_key_rows']} newly empty key row(s); use --accept-empty after review"
                )
            if blocked:
                print_summary(summary)
                print("Export blocked: " + "; ".join(blocked), file=sys.stderr)
                return 1
            receipt = audit.export(args.output, encoding=args.output_encoding)
            receipt["review_id"] = report["review_id"]
            receipt["accepted_collisions"] = args.accept_collisions
            receipt["accepted_empty"] = args.accept_empty
            print(json.dumps(receipt, indent=2, ensure_ascii=True))
            return 0
    raise AuditError("unknown command")


def _main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except BrokenPipeError:
        raise
    except (
        AuditError,
        OSError,
        ValueError,
        csv.Error,
        sqlite3.Error,
        KeyError,
        TypeError,
        RecursionError,
    ) as e:
        # Escaping prevents terminal-control characters in data/paths from executing.
        print(f"datawrench key-audit: {ascii(str(e))[1:-1]}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("datawrench key-audit: interrupted; incomplete temporary output removed", file=sys.stderr)
        return 130


class Terminated(BaseException):
    pass


def main(argv: list[str] | None = None) -> int:
    """Run the command; SIGTERM, closed pipes and argparse exits become exit codes."""

    def terminate(signum, frame):
        raise Terminated

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            code = _main(argv)
        except SystemExit as e:
            code = e.code
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # A consumer such as head closing the pipe is normal CLI control flow.
        # Redirect before interpreter shutdown to prevent a second flush error.
        with open(os.devnull, "w") as sink:
            for stream in (sys.stdout, sys.stderr):
                try:
                    os.dup2(sink.fileno(), stream.fileno())
                except (OSError, ValueError):
                    pass
        return 141
    except Terminated:
        print("datawrench key-audit: terminated; incomplete temporary output removed", file=sys.stderr)
        return 143
    finally:
        signal.signal(signal.SIGTERM, previous)


# Former console-script name of ``main``.
entrypoint = main


if __name__ == "__main__":
    raise SystemExit(main())
