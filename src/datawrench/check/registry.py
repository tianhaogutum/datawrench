"""The list of checks, and the crash-isolating loop that runs them."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Callable, Iterable

from . import checks
from .discover import Discovery, Source
from .model import ERROR, Finding

ISSUES_URL = "https://github.com/tianhaogutum/datawrench/issues"
# Set to 1 to let a crashing check raise its traceback instead of reporting it.
DEBUG_ENV = "DATAWRENCH_CHECK_DEBUG"

DBT = "dbt & SQLMesh"
SQL = "SQL quality"
FRAMES = "DataFrames & files"
SINGER = "Singer pipelines"
INPUTS = "Inputs"
AREAS = (DBT, SQL, FRAMES, SINGER, INPUTS)


@dataclass(frozen=True)
class CheckOptions:
    # Manifest of the main branch / production run that contract-diff compares against.
    base_manifest: Path | None = None
    # (source dialect or None to infer it from the dbt manifest, target dialect) for dialect-audit.
    dialect: tuple[str | None, str] | None = None
    # Empty-table DDL or YAML fixture for dialect-audit's DuckDB binding.
    sql_schema: Path | None = None


@dataclass(frozen=True)
class Check:
    name: str
    area: str
    summary: str
    kinds: frozenset[str]
    # Per-input checks take (source, options); cross-input checks take (sources, options).
    run: Callable[..., Iterable[Finding]] | None = None
    cross_file: bool = False
    rules: tuple[str, ...] = field(default=())
    # Top-level import names of optional dependencies, and the extra that installs them.
    requires: tuple[str, ...] = ()
    extra: str | None = None
    # What check needs from you before it can run this fully (shown by --list-checks).
    note: str | None = None

    @property
    def tool(self) -> str:
        return "-" if self.name == "parse" else self.name


CHECKS: tuple[Check, ...] = (
    Check("contract-diff", DBT,
          "dbt manifests: breaking changes to enforced model contracts (removed columns, narrowed or incompatible "
          "types, dropped constraints) against a base manifest",
          frozenset({"dbt-manifest"}), checks.contract_diff, cross_file=True,
          rules=("breaking", "non-breaking", "warning", "notice", "summary", "needs-base", "no-head",
                 "invalid-base", "unsupported-manifest"),
          requires=("sqlglot",), extra="sqlglot",
          note="compares only with --base-manifest PATH"),
    Check("sqlmesh-manifest", DBT,
          "SQLMesh projects: the exported dbt manifest validates (official v12 schema, dbt-artifacts-parser, "
          "graph links), or a hint to export one",
          frozenset({"sqlmesh-project", "dbt-manifest"}), checks.sqlmesh_manifest, cross_file=True,
          rules=("invalid-manifest", "invalid-json", "valid", "not-exported"),
          requires=("sqlmesh", "dbt_artifacts_parser", "jsonschema", "pydantic"), extra="sqlmesh"),
    Check("lint-cache", SQL,
          "SQL files under a SQLFluff config: SQLFluff violations per rule, parse and templating errors "
          "(no cache is written)",
          frozenset({"sql", "sqlfluff-config"}), checks.lint_cache, cross_file=True,
          rules=("<SQLFluff rule code>", "PRS", "TMP", "needs-config", "templater-not-run", "config-error",
                 "summary"),
          requires=("sqlfluff", "jinja2"), extra="sqlfluff",
          note="needs your SQLFluff config (.sqlfluff with a dialect); never runs the dbt templater"),
    Check("dialect-audit", SQL,
          "SQL files: statements that will not translate between two dialects (parse errors, opaque commands, "
          "unknown functions, unsupported or unstable output)",
          frozenset({"sql", "dbt-manifest"}), checks.dialect_audit, cross_file=True,
          rules=("parse-error", "command-fallback", "unsupported", "anonymous-function", "roundtrip-unstable",
                 "bind-error", "unknown-table", "jinja-skipped", "empty-input", "input-skipped", "needs-dialect",
                 "invalid-config", "summary"),
          requires=("sqlglot", "duckdb", "yaml"), extra="sqlglot,duckdb",
          note="needs --dialect FROM:TO (FROM can come from the dbt manifest: --dialect :duckdb)"),
    Check("polars-vet", FRAMES,
          "Python files that import polars: eager reads turned lazy, Python lambdas, row loops, with_columns or "
          "concat in loops, sort+head, pandas round trips",
          frozenset({"python-polars"}), checks.polars_vet,
          rules=("PLV001", "PLV002", "PLV003", "PLV004", "PLV005", "PLV006", "PLV007", "PLV008", "PLV009",
                 "PLV010", "PLV011", "PLV000", "unreadable")),
    Check("parquet-unify", FRAMES,
          "Parquet folders: schema drift between files (type conflicts, widenable types, missing columns), "
          "unreadable footers",
          frozenset({"parquet-dataset"}), checks.parquet_unify,
          rules=("type-conflict", "widenable", "missing-column", "unreadable", "more-columns", "schema-groups",
                 "consistent"),
          requires=("pyarrow",), extra="parquet"),
    Check("seedtypes", FRAMES,
          "dbt seeds and CSV files: columns whose default DuckDB or Polars read changes the text (leading zeros, "
          "decimals) and are not pinned in column_types",
          frozenset({"dbt-project", "csv"}), checks.seedtypes,
          rules=("unpinned-lossy", "reader-failed", "not-checked", "summary", "lossy-default"),
          requires=("duckdb", "fsspec", "polars", "yaml"), extra="duckdb,polars"),
    Check("duckmate", FRAMES,
          "dbmate-style DuckDB migrations: invalid files, sections DuckDB cannot parse or duckmate refuses, "
          "empty down sections",
          frozenset({"duckmate-migrations"}), checks.duckmate,
          rules=("invalid-migration", "parse-error", "unsupported-statement", "empty-down", "summary",
                 "not-duckdb"),
          requires=("duckdb",), extra="duckdb"),
    Check("tapcheck", SINGER,
          "Singer / Airbyte JSONL and catalogs: missing or null primary keys, schema violations, undeclared "
          "streams, missing final STATE",
          frozenset({"singer-messages", "singer-catalog"}), checks.tapcheck,
          rules=("TC001", "TC003", "TC005", "TC006", "TC008", "TC009", "TC010", "TC0xx", "summary", "catalog"),
          requires=("jsonschema", "referencing", "rfc3339_validator"), extra="singer"),
    Check("singer-replay", SINGER,
          "singer-replay cassettes: checksums, catalog and message counts still match the manifest",
          frozenset({"singer-cassette"}), checks.singer_replay,
          rules=("invalid-cassette", "verified")),
    Check("parse", INPUTS, "Inputs that cannot be read or parsed",
          frozenset(), rules=("invalid-json", "invalid-encoding", "unreadable", "unrecognised")),
)
BY_NAME = {check.name: check for check in CHECKS}


def select(only: Iterable[str] = (), skip: Iterable[str] = ()) -> list[Check]:
    """Checks to run, in registry order. Raises ValueError for unknown names."""
    only, skip = list(only), list(skip)
    unknown = sorted({name for name in [*only, *skip] if name not in BY_NAME})
    if unknown:
        raise ValueError(f"unknown check(s): {', '.join(unknown)} (see --list-checks)")
    return [check for check in CHECKS if (not only or check.name in only) and check.name not in skip]


def _internal(check: Check, exc: BaseException, source: Source | None) -> Finding:
    # Only the exception type: messages of unexpected errors may quote input content.
    return Finding(check.name, "internal", ERROR,
                   f"the {check.name} check crashed ({type(exc).__name__}); its results are missing, "
                   "other checks still ran", source.display if source else None,
                   hint=f"please report it at {ISSUES_URL} (set {DEBUG_ENV}=1 for the traceback)")


class _Missing(Exception):
    """An optional dependency is absent; ``produced`` holds the findings yielded before that."""

    def __init__(self, module: str, produced: list[Finding]):
        super().__init__(module)
        self.module = module
        self.produced = produced


def _guarded(check: Check, args: tuple, source: Source | None) -> list[Finding]:
    """Findings of one check call; raises _Missing when an optional dependency is absent."""
    produced: list[Finding] = []
    try:
        for finding in check.run(*args):
            produced.append(finding)
        return produced
    except checks.MissingDependency as exc:
        raise _Missing(exc.module, produced) from None
    except ModuleNotFoundError as exc:
        root = (exc.name or "").partition(".")[0]
        if root in check.requires:
            raise _Missing(root, produced) from None
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        return produced + [_internal(check, exc, source)]
    except Exception as exc:  # noqa: BLE001 - one broken checker must not abort the run
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        return produced + [_internal(check, exc, source)]


def run_checks(discovery: Discovery, selected: Iterable[Check] | None = None,
               options: CheckOptions | None = None) -> list[Finding]:
    """Run ``selected`` checks (default: all) over discovered inputs; never raises for a checker bug."""
    selected = list(CHECKS if selected is None else selected)
    options = options or CheckOptions()
    names = {check.name for check in selected}
    findings = [finding for finding in discovery.findings if finding.check in names]
    for check in selected:
        if check.run is None:
            continue
        relevant = [source for source in discovery.sources if source.kind in check.kinds]
        if not relevant:
            continue
        if check.cross_file:
            try:
                findings.extend(_guarded(check, (relevant, options), None))
            except _Missing as missing:
                findings.extend(missing.produced)
                findings.append(checks.missing_dependency(check.name, missing.module, check.extra, len(relevant)))
            continue
        for index, source in enumerate(relevant):
            try:
                findings.extend(_guarded(check, (source, options), source))
            except _Missing as missing:
                # Findings already produced are kept; the remaining inputs become one notice.
                findings.extend(missing.produced)
                findings.append(checks.missing_dependency(check.name, missing.module, check.extra,
                                                          len(relevant) - index))
                break
    return sorted(findings, key=Finding.sort_key)
