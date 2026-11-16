"""Command-line interface. Diagnostics and cache stats never contaminate JSON."""
from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

from sqlfluff.core.errors import SQLFluffUserError

from . import __version__
from .cache import Cache
from .engine import lint
from .fingerprint import InputError


def annotations(records: list[dict]) -> list[dict]:
    output = []
    for record in records:
        for v in record["violations"]:
            output.append({"file": record["filepath"],
                           "start_line": v["start_line_no"], "start_column": v["start_line_pos"],
                           "end_line": v.get("end_line_no", v["start_line_no"]),
                           "end_column": v.get("end_line_pos", v["start_line_pos"]),
                           "title": "SQLFluff",
                           "message": f"{v['code']}: {v['description']}",
                           "annotation_level": "notice" if v["warning"] else "warning"})
    return output


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="datawrench lint-cache", description="Content-addressed SQLFluff lint cache")
    cli.add_argument("--version", action="version", version=__version__)
    sub = cli.add_subparsers(dest="command", required=True)
    lint_cli = sub.add_parser("lint", help="Lint SQL with SQLFluff's own engine")
    lint_cli.add_argument("paths", nargs="*", default=[])
    lint_cli.add_argument("--dialect")
    lint_cli.add_argument("--config", dest="config_path")
    lint_cli.add_argument("--format", choices=("human", "json", "github-annotation", "cache-json"), default="human")
    lint_cli.add_argument("--no-cache", action="store_true")
    lint_cli.add_argument("--stats", action="store_true")
    lint_cli.add_argument("--explain", action="store_true",
                          help="Explain per-file cache decisions on stderr (no input values)")
    lint_cli.add_argument("--explain-dependencies", action="store_true",
                          help="Include dependency subcategories (implies --explain; no names/values)")
    lint_cli.add_argument("--dbt-project-hash", action="store_true",
                          help="Opt in to dbt cache keyed on project, profiles and environment")
    lint_cli.add_argument("--trust-template-code", action="store_true",
                          help="Authorize deterministic Python libraries/dbt to execute (trusted inputs only)")
    for name in ("status", "clear", "gc"):
        child = sub.add_parser(name, help={"status": "Report result/history counts and managed bytes as JSON",
                                          "clear": "Remove managed entries only",
                                          "gc": "Plan or remove invalid, old and over-budget entries"}[name])
        child.add_argument("--cache-dir", default=".sqlfluff_cache")
        if name == "status":
            child.add_argument("--fail-on-corrupt", action="store_true",
                               help="Exit 1 after reporting if results or histories are structurally corrupt")
            child.add_argument("--details", action="store_true",
                               help="Include ordered managed names, bytes and structural corruption flags")
            child.add_argument("--corrupt-reasons", action="store_true",
                               help="Add fixed corruption category codes (requires --details)")
            child.add_argument("--corrupt-counts", action="store_true",
                               help="Summarize fixed corruption categories by kind without requiring names")
            child.add_argument("--corrupt-bytes", action="store_true",
                               help="Include logical bytes by corruption category (requires --corrupt-counts)")
        if name == "gc":
            child.add_argument("--fail-on-planned", action="store_true",
                               help="Exit 1 if any results or histories are planned for removal (requires --dry-run)")
            child.add_argument("--summary", action="store_true",
                               help="Replace the per-name plan with aggregate planned reason counts")
            child.add_argument("--reason-bytes", action="store_true",
                               help="Add planned logical bytes by reason (requires --summary)")
            child.add_argument("--max-age-days", type=float, default=30)
            child.add_argument("--max-bytes", type=nonnegative_bytes,
                               help="Maximum managed result + history bytes after GC")
        if name in ("clear", "gc"):
            if name == "clear":
                child.add_argument("--summary", action="store_true",
                                   help="Omit managed names from the preview (requires --dry-run)")
            child.add_argument("--dry-run", action="store_true",
                               help="Print the ordered JSON plan without deleting or writing files")
    lint_cli.add_argument("--cache-dir", default=".sqlfluff_cache")
    return cli


def nonnegative_bytes(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--max-bytes must be a nonnegative integer") from None
    if result < 0:
        raise argparse.ArgumentTypeError("--max-bytes must be a nonnegative integer")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    def warn(message: str) -> None:
        print(f"datawrench lint-cache: {message}", file=sys.stderr)
    try:
        if args.command != "lint":
            cache = Cache(Path(args.cache_dir), warn)
            if args.command == "status":
                report = cache.status(details=args.details,
                                      corrupt_reasons=args.corrupt_reasons,
                                      corrupt_counts=args.corrupt_counts,
                                      corrupt_bytes=args.corrupt_bytes)
                print(json.dumps(report, sort_keys=True))
                return int(args.fail_on_corrupt and (report["corrupt"] > 0 or
                                                    report["history_corrupt"] > 0))
            elif args.command == "clear":
                if args.summary and not args.dry_run:
                    raise InputError("--summary requires --dry-run")
                print(json.dumps(cache.plan_clear(summary=args.summary) if args.dry_run
                                 else {"removed": cache.clear()}))
            else:
                if args.fail_on_planned and not args.dry_run:
                    raise InputError("--fail-on-planned requires --dry-run")
                report = cache.collect(args.max_age_days, max_bytes=args.max_bytes,
                                       dry_run=args.dry_run, summary=args.summary,
                                       reason_bytes=args.reason_bytes)
                print(json.dumps(report))
                return int(args.fail_on_planned and (report["planned_entries"] > 0 or
                                                    report["planned_histories"] > 0))
            return 0
        result = lint(args.paths, dialect=args.dialect, config_path=args.config_path,
                      cache_dir=args.cache_dir, no_cache=args.no_cache,
                      dbt_project_hash=args.dbt_project_hash,
                      trust_template_code=args.trust_template_code,
                      explain_dependencies=args.explain_dependencies, warn=warn)
        if args.format == "cache-json":
            print(json.dumps({"files": result.explanations, "stats": result.stats.to_dict()},
                             ensure_ascii=True))
        elif args.format == "json":
            print(json.dumps(result.records, ensure_ascii=True))
        elif args.format == "github-annotation":
            print(json.dumps(annotations(result.records), ensure_ascii=True))
        else:
            for record in result.records:
                print(f"== {record['filepath']} == {'FAIL' if any(not v['warning'] for v in record['violations']) else 'PASS'}")
                for v in record["violations"]:
                    level = "warning" if v["warning"] else "error"
                    print(f"L:{v['start_line_no']:4} | P:{v['start_line_pos']:4} | {v['code']} | {level}: {v['description']}")
        if args.explain or args.explain_dependencies:
            for item in result.explanations:
                changes = item.get("dependency_changes", [])
                warn(f"{item['action']} {json.dumps(item['filepath'], ensure_ascii=True)}: "
                     + ", ".join(item["reasons"])
                     + (" [" + ", ".join(changes) + "]" if changes else ""))
        if args.stats or args.explain or args.explain_dependencies:
            print("datawrench lint-cache: " + " ".join(f"{k}={v}" for k, v in result.stats.to_dict().items()),
                  file=sys.stderr)
        return result.exit_code
    except (InputError, SQLFluffUserError, OSError, ValueError, configparser.Error) as exc:
        warn(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
