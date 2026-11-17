"""``datawrench check [PATH ...]``: scan a data repository, report what will break before the warehouse does.

Exit status: 0 no finding at the --fail-on threshold, 1 findings at or above
it, 2 usage or input error (unknown option or check, missing path).
Read-only: no file is written, no model, tap or query is run, no network is used.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

from .discover import DiscoveryError, discover
from .registry import AREAS, CHECKS, CheckOptions, run_checks, select
from .report import RENDERERS, Run, render_text

PROG = "datawrench check"
FAIL_ON = {"error": "error", "warning": "warning", "never": None}
DIALECT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

EPILOG = """\
examples:
  datawrench check                                   scan the current directory
  datawrench check . --base-manifest prod/manifest.json
  datawrench check models/ target/compiled --dialect snowflake:duckdb
  datawrench check . --format markdown >> "$GITHUB_STEP_SUMMARY"
  datawrench check . --format sarif > datawrench.sarif
  datawrench check . --only polars-vet,seedtypes --fail-on warning
  datawrench check --list-checks

exit status: 0 nothing at the --fail-on level, 1 findings at or above it,
2 usage or input error. Nothing is written, executed or sent anywhere.
Docs: https://github.com/tianhaogutum/datawrench/blob/main/docs/check.md
"""


def _names(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("expected one or more check names, comma separated")
    return names


def parse_dialect(value: str) -> tuple[str | None, str]:
    """``snowflake:duckdb`` -> ("snowflake", "duckdb"); ``:duckdb`` infers the source from the dbt manifest."""
    source, sep, target = value.partition(":")
    source, target = source.strip().lower(), target.strip().lower()
    if not sep or not target or not DIALECT_NAME.fullmatch(target) or (source and not DIALECT_NAME.fullmatch(source)):
        raise argparse.ArgumentTypeError("expected FROM:TO, e.g. snowflake:duckdb (or :duckdb to take FROM from "
                                         "the dbt manifest's adapter)")
    return source or None, target


def _file(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"not a file: {value}")
    return path


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Find dbt projects and manifests, SQL, Polars scripts, CSV seeds, Parquet folders, DuckDB "
                    "migrations, join and grain contracts and Singer output under PATH and report breaking contract "
                    "changes, lossy seed types, Parquet schema drift, slow Polars patterns, untranslatable SQL, "
                    "fan-out joins, grain contracts that no longer compile and broken tap output.")
    p.add_argument("paths", nargs="*", metavar="PATH", help="folders or files to scan (default: .)")
    p.add_argument("--format", choices=("text", "json", "sarif", "markdown"), default="text",
                   help="report format (default: text); markdown suits a PR comment or $GITHUB_STEP_SUMMARY, "
                        "sarif is SARIF 2.1.0 for code scanning")
    p.add_argument("--fail-on", choices=tuple(FAIL_ON), default="error",
                   help="exit 1 when a finding is at least this severe (default: error)")
    p.add_argument("--only", type=_names, action="append", default=[], metavar="CHECK[,CHECK]",
                   help="run only these checks (repeatable)")
    p.add_argument("--skip", type=_names, action="append", default=[], metavar="CHECK[,CHECK]",
                   help="do not run these checks (repeatable)")
    p.add_argument("--list-checks", action="store_true", help="list the available checks and exit")
    p.add_argument("--base-manifest", type=_file, metavar="PATH",
                   help="dbt manifest.json of the main branch or production run; contract-diff compares "
                        "every other manifest found against it")
    p.add_argument("--dialect", type=parse_dialect, metavar="FROM:TO",
                   help="SQL dialect pair for dialect-audit, e.g. snowflake:duckdb; ':duckdb' takes FROM from "
                        "the dbt manifest's adapter (default: dialect-audit does not run)")
    p.add_argument("--sql-schema", type=_file, metavar="FILE",
                   help="empty-table DDL or YAML fixture: with --dialect ...:duckdb, dialect-audit also binds "
                        "each query in DuckDB (nothing is executed)")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="colour text output (default: auto; honours NO_COLOR)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="text: show a fix hint under each finding and list inputs without findings")
    return p


def list_checks() -> str:
    width = max(len(check.name) for check in CHECKS) + 2
    lines = ["Checks (select with --only/--skip):"]
    for area in AREAS:
        group = [check for check in CHECKS if check.area == area]
        if not group:
            continue
        lines += ["", f"{area}:"]
        for check in group:
            lines.append(f"  {check.name.ljust(width)}{check.summary}")
            notes = ["built in" if check.tool == "-" else f"uses: datawrench {check.tool}",
                     f"rules: {', '.join(check.rules)}"]
            if check.note:
                notes.append(check.note)
            if check.extra:
                notes.append(f"needs datawrench[{check.extra}]")
            lines.append(f"  {''.ljust(width)}{'; '.join(notes)}")
    return "\n".join(lines) + "\n"


def _use_color(choice: str) -> bool:
    if choice != "auto":
        return choice == "always"
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.list_checks:
        sys.stdout.write(list_checks())
        return 0
    try:
        checks = select([name for group in args.only for name in group],
                        [name for group in args.skip for name in group])
        if not checks:
            raise ValueError("--only/--skip leave no check to run")
        if args.sql_schema and not args.dialect:
            raise ValueError("--sql-schema needs --dialect FROM:duckdb")
        options = CheckOptions(base_manifest=args.base_manifest, dialect=args.dialect, sql_schema=args.sql_schema)
        paths = args.paths or ["."]
        discovery = discover(paths)
    except (ValueError, DiscoveryError) as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return 2
    findings = run_checks(discovery, checks, options)
    run = Run(paths, discovery.sources, discovery.scanned, checks, findings, FAIL_ON[args.fail_on])
    if args.format == "text":
        output = render_text(run, color=_use_color(args.color), verbose=args.verbose)
    else:
        output = RENDERERS[args.format](run)
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 1 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
