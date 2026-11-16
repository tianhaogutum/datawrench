from __future__ import annotations

import argparse
from collections import Counter
import json
import sys

from pyarrow import ArrowException

from . import __version__
from .catalog import plan, plan_report, scan
from .operations import rewrite, verify
from .recipes import duckdb_recipe, polars_recipe
from .types import Conflict, UnifyError, schema_spec
from .validation import validate_artifact


def render(value, fmt):
    if fmt == "json":
        return json.dumps(value, indent=2, ensure_ascii=True)
    if "columns" in value and "groups" in value:
        lines = [f"{value['files']} files; {len(value['groups'])} schema groups; {value['rows']} rows"]
        path_counts = Counter(c["path"] for c in value["columns"])
        if fmt == "md":
            lines += ["", "| Path (JSON pointer) | Classification | Types | Unified |", "|---|---|---|---|"]
        for c in value["columns"]:
            path = c["path"]
            if path_counts[path] > 1:
                path += " (parts=" + json.dumps(c["path_parts"], ensure_ascii=True) + ")"
            types = " / ".join(c["types"])
            label = c["classification"] + (" + missing_in_some" if c["missing_in_some"] and c["classification"] != "missing_in_some" else "")
            if fmt == "md":
                cells = [path, label, types, c["unified"] or c["reason"] or "-"]
                lines.append("| " + " | ".join(x.replace("|", "\\|").replace("\n", " ") for x in cells) + " |")
            else:
                lines.append(f"{path}: {label}: {types} -> {c['unified'] or c['reason']}")
        for u in value["unreadable"]:
            lines.append(f"UNREADABLE {u['file']}: {u['error']}")
        if "row_groups_by_file" in value:
            inventory = json.dumps(value["row_groups_by_file"], indent=2, ensure_ascii=True)
            note = "Footer total_byte_size is layout evidence, not an RSS or safe-memory estimate."
            if fmt == "md":
                lines += ["", "### Physical row groups", "", note, "", "```json", inventory, "```"]
            else:
                lines += ["", "Physical row groups", note, inventory]
        return "\n".join(lines)
    text = json.dumps(value, indent=2, ensure_ascii=True)
    return "```json\n" + text + "\n```" if fmt == "md" else text


def parser():
    root = argparse.ArgumentParser(prog="datawrench parquet-unify", description="Audit and reconcile local Parquet schemas, with cross-engine verification.")
    root.add_argument("--version", action="version", version=__version__)
    subs = root.add_subparsers(dest="command", required=True)
    for name in ("scan", "plan", "recipe", "verify", "rewrite", "validate"):
        p = subs.add_parser(name)
        p.add_argument("path", help="local file or directory (recursive); no URLs or input globs")
        p.add_argument("--hive", action="store_true", help="materialize key=value partitions as string columns")
        p.add_argument("--skip-unreadable", action="store_true", help="explicitly exclude unreadable files, with a warning")
        if name != "recipe":
            p.add_argument("--format", choices=("text", "json", "md"), default="text" if name in ("scan", "plan") else "json")
        if name == "scan":
            p.add_argument("--exit-code", action="store_true", help="exit 1 for schema drift, 2 for unreadable/invalid input")
            p.add_argument("--row-groups", action="store_true", help="include per-file physical row-group layout from footers (not memory bounds)")
        else:
            p.add_argument("--policy", choices=("widen", "strict", "stringify-conflicts"), default="widen")
        if name == "plan":
            p.add_argument("--explain", action="store_true", help="include all field/file lossless bounds, even when planning fails")
        if name == "recipe":
            p.add_argument("--engine", choices=("duckdb", "polars"), required=True)
        if name == "rewrite":
            p.add_argument("--out", required=True, help="new directory outside the input tree; parent must exist")
        if name == "validate":
            p.add_argument("--output", required=True, help="existing local artifact file/directory; never modified or skipped")
            p.add_argument("--null-column", action="append", default=[], help="literal top-level column name; repeat to check more")
            p.add_argument("--checksum-column", action="append", default=[],
                           help="literal top-level column name for typed value checksum; repeat to check more")
            p.add_argument("--row-checksum-column", action="append", default=[],
                           help="literal top-level name in a typed row tuple; repeat in tuple order (duplicates ignored)")
            p.add_argument("--ordered-row-checksum-column", action="append", default=[],
                           help="literal top-level name in an order-sensitive row tuple; repeat in tuple order (duplicates ignored)")
            p.add_argument("--row-group-checksum-evidence", action="store_true",
                           help="with --ordered-row-checksum-column, add per-physical-row-group ordered digests (no extra decoding)")
            p.add_argument("--batch-size", type=int, default=65536, help="positive decoded rows per batch (not a byte cap)")
            p.add_argument("--max-rows", type=int, help="reject either side above this footer row count before decoding")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        catalog = scan(args.path, hive=args.hive, skip_unreadable=args.skip_unreadable,
                       row_groups=getattr(args, "row_groups", False))
        if args.command == "scan":
            report = catalog.report()
            print(render(report, args.format))
            if catalog.unreadable and not args.skip_unreadable:
                return 2
            drift = len(catalog.groups()) > 1 or any(c["classification"] == "conflict" for c in report["columns"])
            return 1 if args.exit_code and drift else 0
        if catalog.unreadable:
            details = json.dumps(catalog.unreadable, ensure_ascii=True)
            if not args.skip_unreadable:
                raise UnifyError("unreadable files (use --skip-unreadable to exclude): " + details)
            print("warning: excluding unreadable files: " + details, file=sys.stderr)
        if args.command == "plan" and args.explain:
            output = plan_report(catalog, args.policy)
            print(render(output, args.format))
            if not output["ok"]:
                print(f"conflict: {output['error']}", file=sys.stderr)
            return 0 if output["ok"] else 1
        target = plan(catalog, args.policy)
        if args.command == "plan":
            output = {"policy": args.policy, "schema": schema_spec(target), "unreadable": catalog.unreadable}
        elif args.command == "recipe":
            print((duckdb_recipe if args.engine == "duckdb" else polars_recipe)(catalog, target), end="")
            return 0
        elif args.command == "verify":
            output = verify(catalog, target)
            output["unreadable"] = catalog.unreadable
            print(render(output, args.format))
            return 0 if output["ok"] else 1
        elif args.command == "validate":
            output = validate_artifact(catalog, target, args.output, null_columns=args.null_column,
                                       checksum_columns=args.checksum_column,
                                       row_checksum_columns=args.row_checksum_column,
                                       ordered_row_checksum_columns=args.ordered_row_checksum_column,
                                       batch_size=args.batch_size, max_rows=args.max_rows,
                                       row_group_checksum_evidence=args.row_group_checksum_evidence)
            output["policy"] = args.policy
            print(render(output, args.format))
            return 0 if output["ok"] else 1
        else:
            output = rewrite(catalog, target, args.out)
            output["unreadable"] = catalog.unreadable
        print(render(output, args.format))
        return 0
    except Conflict as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return 1
    except (UnifyError, OSError, ValueError, TypeError, ArrowException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
