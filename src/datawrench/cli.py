"""Top-level ``datawrench`` command: dispatches to one tool per subcommand.

Each tool lives in its own subpackage exposing ``cli.main(argv) -> int``. Tools
are imported lazily, so ``datawrench --help`` stays fast and a broken or missing
tool only affects its own command. Tools with heavy third-party dependencies
declare them in ``requires``; when one is not installed the command prints a
one-line install hint for the matching extra(s) instead of a traceback.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import importlib
import importlib.util
import sys

from . import __version__

PROG = "datawrench"
REPO_URL = "https://github.com/tianhaogutum/datawrench"


@dataclass(frozen=True)
class Command:
    name: str
    # Dotted path of the module exposing ``main(argv)``.
    module: str
    summary: str
    group: str
    # Top-level import names of optional dependencies, and the extra(s) that install them
    # (comma-separated, exactly as written inside ``datawrench[...]``).
    requires: tuple[str, ...] = ()
    extra: str | None = None


GROUPS = ("Check", "dbt & SQLMesh", "SQL quality", "Keys & joins", "DataFrames & files", "Singer pipelines")

COMMANDS: tuple[Command, ...] = (
    Command("check", "datawrench.check.cli",
            "Scan a data project and report what would break before it hits the warehouse", "Check"),
    Command("contract-diff", "datawrench.contract_diff.cli",
            "Type-aware compatibility check between two dbt manifest model contracts", "dbt & SQLMesh",
            requires=("sqlglot",), extra="sqlglot"),
    Command("sqlmesh-manifest", "datawrench.sqlmesh_manifest.cli",
            "Export SQLMesh project metadata as an offline dbt manifest v12; validate and diff it",
            "dbt & SQLMesh",
            requires=("sqlmesh", "sqlglot", "dbt_artifacts_parser", "jsonschema", "referencing",
                      "ruamel", "pydantic"),
            extra="sqlmesh"),
    Command("lint-cache", "datawrench.lint_cache.cli",
            "Content-addressed per-file cache for SQLFluff lint results", "SQL quality",
            requires=("sqlfluff", "jinja2"), extra="sqlfluff"),
    Command("dialect-audit", "datawrench.dialect_audit.cli",
            "Per-statement SQL dialect migration report with offline DuckDB binding checks",
            "SQL quality", requires=("sqlglot", "duckdb"), extra="sqlglot,duckdb"),
    Command("rebuild-check", "datawrench.rebuild_check.cli",
            "Rehearse SQLite migrations on a backup; flag triggers, indexes and keys a rebuild dropped",
            "SQL quality", requires=("sqlite_utils", "sqlparse"), extra="sqlite"),
    Command("join-witness", "datawrench.join_witness.cli",
            "Find the source records that multiply a join and how they change a sum (TableProof)",
            "Keys & joins", requires=("tableproof",), extra="tableproof"),
    Command("grain-contract", "datawrench.grain_contract.cli",
            "Compile SELECT SQL, primary keys and join-grain declarations into zero-row SQL tests",
            "Keys & joins", requires=("sqlglot", "ijson"), extra="sqlglot,ijson"),
    Command("key-audit", "datawrench.key_audit.cli",
            "Preview Unicode key cleanup: find CSV keys that would newly collide, export confirmed rules",
            "Keys & joins", requires=("ftfy",), extra="ftfy"),
    Command("polars-vet", "datawrench.polars_vet.cli",
            "Static, alias-aware performance checks for Polars code (also a flake8 plugin)",
            "DataFrames & files"),
    Command("parquet-unify", "datawrench.parquet_unify.cli",
            "Audit and reconcile drifting Parquet schemas, verified across DuckDB and Polars",
            "DataFrames & files", requires=("pyarrow", "duckdb", "polars"),
            extra="parquet,duckdb,polars"),
    Command("seedtypes", "datawrench.seedtypes.cli",
            "Lossless CSV/seed type inference with pinned dbt, DuckDB and Polars recipes",
            "DataFrames & files", requires=("duckdb", "fsspec", "polars", "sqlglot"),
            extra="duckdb,polars,sqlglot"),
    Command("duckmate", "datawrench.duckmate.cli",
            "dbmate-format DuckDB migrations with schema snapshots and drift verification",
            "DataFrames & files", requires=("duckdb",), extra="duckdb"),
    Command("csv-rewind", "datawrench.csv_rewind.cli",
            "Show which CSV cells a proposed DuckDB schema would change; freeze a verified reader",
            "DataFrames & files", requires=("duckdb",), extra="duckdb"),
    Command("failurepack", "datawrench.failurepack.cli",
            "Shrink a CSV to a replayable bundle that still triggers one chosen Pandera failure",
            "DataFrames & files", requires=("pandera", "pandas", "dfshrink", "polars", "packaging"),
            extra="pandera"),
    Command("tapcheck", "datawrench.tapcheck.cli",
            "Streaming contract and data-quality checks for Singer and Airbyte JSONL",
            "Singer pipelines", requires=("jsonschema", "referencing", "rfc3339_validator"),
            extra="singer"),
    Command("singer-replay", "datawrench.singer_replay.cli",
            "Record scrubbed Singer cassettes and replay them as a connection-free tap",
            "Singer pipelines"),
)
BY_NAME = {command.name: command for command in COMMANDS}

# Former standalone tool names, used only to point users at the new subcommand.
LEGACY = {
    "dbt-contract-diff": "contract-diff", "sqlmesh-dbt-manifest": "sqlmesh-manifest",
    "sqlfluff-cache": "lint-cache", "tap-replay": "singer-replay tap",
    "sqlite-rebuild-check": "rebuild-check", "sqlite-utils-rebuild-check": "rebuild-check",
    "keyaudit": "key-audit", "unicode-key-audit": "key-audit", "sql-grain-contract": "grain-contract",
    "tableproof-join-witness": "join-witness",
}


def _present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _installed(command: Command) -> bool:
    return _present(command.module.rpartition(".")[0])


def missing_dependency_message(command: Command, dependency: str) -> str:
    extra = f"{PROG}[{command.extra}]"
    return (f"{PROG} {command.name} needs {dependency}: pipx install '{extra}' "
            f"(or pip install '{extra}')\n")


def _missing_requirement(command: Command, exc: ModuleNotFoundError) -> str | None:
    """The optional dependency behind ``exc``, if it is one this command declares."""
    root = (exc.name or "").partition(".")[0]
    return root if root and root in command.requires else None


def overview() -> str:
    width = max(len(command.name) for command in COMMANDS) + 2
    lines = [
        f"{PROG} {__version__} - offline checks for dbt, SQL, joins, CSV/Parquet and Singer pipelines",
        "",
        f"usage: {PROG} <command> [args...]",
        f"       {PROG} <command> --help",
    ]
    for group in GROUPS:
        lines += ["", f"{group}:"]
        for command in COMMANDS:
            if command.group != group:
                continue
            note = "" if _installed(command) else "  (coming soon)"
            lines.append(f"  {command.name.ljust(width)}{command.summary}{note}")
    lines += [
        "",
        "options:",
        "  -h, --help     show this overview",
        "  -V, --version  show the version and exit",
        "",
        f"Optional extras: {PROG}[sqlglot] (contract-diff), {PROG}[sqlmesh] (sqlmesh-manifest),",
        f"{PROG}[sqlfluff] (lint-cache), {PROG}[sqlglot,duckdb] (dialect-audit),",
        f"{PROG}[parquet,duckdb,polars] (parquet-unify), {PROG}[duckdb,polars,sqlglot] (seedtypes),",
        f"{PROG}[duckdb] (duckmate, csv-rewind), {PROG}[singer] (tapcheck), {PROG}[sqlite] (rebuild-check),",
        f"{PROG}[tableproof] (join-witness), {PROG}[sqlglot,ijson] (grain-contract), {PROG}[ftfy] (key-audit),",
        f"{PROG}[pandera] (failurepack, Python 3.12+), or {PROG}[all] (all but sqlmesh and pandera).",
        "",
        f"Docs and issues: {REPO_URL}",
    ]
    return "\n".join(lines) + "\n"


def _unknown(name: str) -> int:
    message = f"{PROG}: unknown command {name!r}"
    if name in LEGACY:
        message += f"; {name} is now '{PROG} {LEGACY[name]}'"
    else:
        match = difflib.get_close_matches(name, [*BY_NAME, *LEGACY], n=1, cutoff=0.6)
        if match:
            suggestion = LEGACY.get(match[0], match[0])
            message += f"; did you mean '{suggestion}'?"
    sys.stderr.write(message + f"\nRun '{PROG} --help' to list commands.\n")
    return 2


def run(command: Command, argv: list[str]) -> int:
    if not _installed(command):
        sys.stderr.write(f"{PROG}: command {command.name!r} is not available in "
                         f"{PROG} {__version__} yet; see {REPO_URL}\n")
        return 2
    try:
        module = importlib.import_module(command.module)
        result = module.main(argv)
    except ModuleNotFoundError as exc:
        dependency = _missing_requirement(command, exc)
        if dependency is None:
            raise
        sys.stderr.write(missing_dependency_message(command, dependency))
        return 2
    return 0 if result is None else result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(overview())
        return 0
    first, rest = args[0], args[1:]
    if first in ("-V", "--version"):
        sys.stdout.write(f"{PROG} {__version__}\n")
        return 0
    if first == "help":
        if not rest:
            sys.stdout.write(overview())
            return 0
        first, rest = rest[0], ["--help"]
    if first.startswith("-"):
        sys.stderr.write(f"{PROG}: unknown option {first!r}\nRun '{PROG} --help' for usage.\n")
        return 2
    command = BY_NAME.get(first)
    if command is None:
        return _unknown(first)
    return run(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
