"""The checks. Each one calls the read-only Python API of one datawrench tool.

Per-input checkers are generators ``(source, options) -> Finding...``;
cross-input checkers take ``(sources, options)``. Expected input problems (the
tool's own error type) become findings here; anything else propagates and is
turned into an ``internal`` finding by the registry. Tool packages, and the
heavy optional dependencies behind them (sqlglot, DuckDB, Polars, pyarrow,
SQLMesh, SQLFluff, jsonschema), are imported inside each checker, after
``need()`` has confirmed they are installed; a missing one becomes a single
info finding with the install command.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import importlib.util
import io
import os
from pathlib import Path
from typing import Iterator

from .discover import DUCKMATE_SNAPSHOT, Source
from .model import ERROR, INFO, WARNING, Finding

MESSAGE_LIMIT = 240
# Per Parquet dataset: column findings beyond this are summarised in one line.
MAX_COLUMN_FINDINGS = 10
# dbt adapter_type -> SQLGlot dialect name, where the two differ.
ADAPTER_DIALECTS = {"sqlserver": "tsql", "synapse": "tsql", "fabric": "tsql", "spark": "spark"}
# The dbmate-style migrations table duckmate uses by default.
MIGRATIONS_TABLE = "schema_migrations"


class MissingDependency(Exception):
    """An optional dependency of a check is not installed (one info finding per check)."""

    def __init__(self, module: str):
        super().__init__(module)
        self.module = module


def available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def need(*modules: str) -> None:
    for module in modules:
        if not available(module):
            raise MissingDependency(module)


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _short(value, limit: int = MESSAGE_LIMIT) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _clean(exc, source: Source, *extra: tuple[Path, str]) -> str:
    """Tool messages quote the paths they were given; the report already shows them."""
    text = str(exc)
    pairs = [(source.path, source.display), *extra]
    for path, display in pairs:
        for spelled in sorted({str(path.resolve()), str(path.absolute()), str(path)}, key=len, reverse=True):
            if spelled not in (".", ""):
                text = text.replace(f"{spelled}: ", "").replace(spelled, display)
    return _short(text)


def _child(source: Source, path) -> str:
    """Display path of a file inside a folder input."""
    try:
        relative = Path(path).resolve().relative_to(source.path.resolve())
    except ValueError:
        return str(path)
    return os.path.normpath(os.path.join(source.display, relative))


# --------------------------------------------------------------------------- contract-diff

def contract_diff(sources: list[Source], options) -> Iterator[Finding]:
    base_path = options.base_manifest
    if base_path is None:
        for source in sources:
            enforced = source.meta.get("enforced", 0)
            if enforced:
                yield Finding("contract-diff", "needs-base", INFO,
                              f"{_plural(enforced, 'model')} with an enforced contract; not compared: no base manifest",
                              source.display,
                              hint="pass --base-manifest PATH (the manifest.json of your main branch or production "
                                   "run) to report breaking contract changes")
        return
    base_key = Path(base_path).resolve()
    heads = [source for source in sources if source.path.resolve() != base_key]
    base_display = os.path.normpath(str(base_path))
    if not heads:
        yield Finding("contract-diff", "no-head", INFO,
                      f"no dbt manifest found to compare with {base_display}",
                      hint="run `dbt parse` (or dbt compile) so target/manifest.json exists, then check again")
        return
    need("sqlglot")
    from datawrench.contract_diff.diff import compare_manifests
    from datawrench.contract_diff.manifest import InputError, load_manifest

    try:
        base = load_manifest(base_path)
    except InputError as exc:
        yield Finding("contract-diff", "invalid-base", ERROR, f"base manifest not usable: {_short(exc)}", base_display,
                      hint="pass a dbt manifest.json written by dbt parse, compile or build")
        return
    for source in heads:
        try:
            report = compare_manifests(base, load_manifest(source.path))
        except (InputError, ValueError) as exc:
            yield Finding("contract-diff", "unsupported-manifest", WARNING, f"not compared: {_short(exc)}",
                          source.display)
            continue
        for change in report.changes:
            subject = change.model + (f" › {change.column}" if change.column else "")
            remediation = next((d.get("remediation") for d in change.details if d.get("remediation")), None)
            if change.breaking:
                yield Finding("contract-diff", "breaking", ERROR, _short(change.message), source.display,
                              subject=subject, hint=remediation)
            else:
                yield Finding("contract-diff", "non-breaking", INFO, _short(change.message), source.display,
                              subject=subject)
        for warning in report.warnings:
            yield Finding("contract-diff", "warning", WARNING, _short(warning), source.display)
        for notice in report.notices:
            yield Finding("contract-diff", "notice", INFO, _short(notice), source.display)
        yield Finding("contract-diff", "summary", INFO,
                      f"compared with {base_display} ({report.dialect} types): "
                      f"{_plural(report.models_compared, 'model')} with enforced contracts, "
                      f"{_plural(len(report.changes), 'change')}, {report.breaking_count} breaking",
                      source.display,
                      hint=f"details: datawrench contract-diff --base {base_display} --head {source.display}")


# --------------------------------------------------------------------------- sqlmesh-manifest

def sqlmesh_manifest(sources: list[Source], options) -> Iterator[Finding]:
    projects = [source for source in sources if source.kind == "sqlmesh-project"]
    if not projects:
        return  # dbt wrote its own manifests; only SQLMesh exports are validated
    manifests = [source for source in sources if source.kind == "dbt-manifest"]
    exported: list[Source] = []
    for project in projects:
        root = project.path.resolve()
        mine = [m for m in manifests if m.path.resolve().is_relative_to(root)]
        if not mine:
            yield Finding("sqlmesh-manifest", "not-exported", INFO,
                          "SQLMesh project without an exported dbt manifest; dbt-only tools cannot see it",
                          project.display,
                          hint=f"datawrench sqlmesh-manifest export --project {project.display} --out "
                               f"{os.path.join(project.display, 'target', 'manifest.json')} --deterministic "
                               "(loads the project locally; Python models need --trust-project)")
        for manifest in mine:
            if manifest not in exported:
                exported.append(manifest)
    if not exported:
        return
    need("sqlmesh", "dbt_artifacts_parser", "jsonschema", "pydantic")
    from datawrench.sqlmesh_manifest.validation import ManifestError, read_manifest, validate_manifest

    for source in exported:
        try:
            manifest = read_manifest(source.path)
        except ManifestError as exc:
            yield Finding("sqlmesh-manifest", "invalid-json", ERROR, f"cannot read the manifest: {_clean(exc, source)}",
                          source.display)
            continue
        try:
            validate_manifest(manifest)
        except ManifestError as exc:
            problems = [line for line in str(exc).splitlines() if line.strip()]
            more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
            yield Finding("sqlmesh-manifest", "invalid-manifest", ERROR,
                          "does not validate: " + _short("; ".join(problems[:3])) + more, source.display,
                          hint=f"datawrench sqlmesh-manifest validate {source.display} lists up to ten problems; "
                               "re-export the project")
            continue
        kinds = Counter(node.get("resource_type") for node in (manifest.get("nodes") or {}).values()
                        if isinstance(node, dict))
        edges = sum(len(v) for v in (manifest.get("parent_map") or {}).values())
        yield Finding("sqlmesh-manifest", "valid", INFO,
                      f"valid dbt manifest: {_plural(kinds['model'], 'model')}, {_plural(kinds['seed'], 'seed')}, "
                      f"{_plural(len(manifest.get('sources') or {}), 'source')}, {_plural(edges, 'edge')}",
                      source.display)


# --------------------------------------------------------------------------- lint-cache

TEMPLATER_MARKERS = ("dbt templater", "library_path")


def lint_cache(sources: list[Source], options) -> Iterator[Finding]:
    sql = [source for source in sources if source.kind == "sql"]
    if not sql:
        return
    configs = sorted((source for source in sources if source.kind == "sqlfluff-config"),
                     key=lambda source: len(source.path.resolve().parts), reverse=True)
    groups: dict[int, list[Source]] = defaultdict(list)
    unconfigured = 0
    for item in sql:
        resolved = item.path.resolve()
        index = next((i for i, config in enumerate(configs) if resolved.is_relative_to(config.path.resolve().parent)),
                     None)
        if index is None:
            unconfigured += 1
        else:
            groups[index].append(item)
    if unconfigured:
        yield Finding("lint-cache", "needs-config", INFO,
                      f"{_plural(unconfigured, 'SQL file')} not linted: no SQLFluff configuration "
                      "(.sqlfluff, or a [tool.sqlfluff] / [sqlfluff] section) above them",
                      hint="add a .sqlfluff with `[sqlfluff]` and `dialect = ...`, or run "
                           "`datawrench lint-cache lint DIR --dialect DIALECT`")
    if not groups:
        return
    need("sqlfluff", "jinja2")
    import configparser

    from sqlfluff.core.errors import SQLFluffUserError

    from datawrench.lint_cache.engine import lint
    from datawrench.lint_cache.fingerprint import InputError

    for index, members in sorted(groups.items()):
        config = configs[index]
        displays = {str(item.path.resolve()): item for item in members}
        try:
            # no_cache: check never creates or updates a .sqlfluff_cache directory.
            result = lint(sorted(displays), config_path=str(config.path.resolve()), no_cache=True)
        except InputError as exc:
            text = _clean(exc, config)
            if any(marker in text for marker in TEMPLATER_MARKERS):
                yield Finding("lint-cache", "templater-not-run", INFO,
                              f"{_plural(len(members), 'SQL file')} not linted: {text}", config.display,
                              hint="check never runs project code; run `datawrench lint-cache lint DIR "
                                   "--trust-template-code` yourself if you trust this project")
            else:
                yield Finding("lint-cache", "config-error", WARNING, f"SQLFluff could not run: {text}",
                              config.display)
            continue
        except (SQLFluffUserError, ValueError, configparser.Error) as exc:
            yield Finding("lint-cache", "config-error", WARNING, f"SQLFluff could not run: {_clean(exc, config)}",
                          config.display)
            continue
        flagged = 0
        for record in result.records:
            item = displays.get(str(Path(record["filepath"]).resolve()))
            display = item.display if item else record["filepath"]
            by_code: dict[str, list[dict]] = {}
            for violation in record["violations"]:
                by_code.setdefault(violation["code"], []).append(violation)
            if by_code:
                flagged += 1
            for code, violations in sorted(by_code.items()):
                first = min(violations, key=lambda v: (v.get("start_line_no") or 0, v.get("start_line_pos") or 0))
                if code in ("PRS", "TMP"):
                    severity = ERROR
                elif all(v.get("warning") for v in violations):
                    severity = INFO
                else:
                    severity = WARNING
                times = f" (×{len(violations)})" if len(violations) > 1 else ""
                yield Finding("lint-cache", code, severity, _short(first.get("description") or code) + times,
                              display, line=first.get("start_line_no") or None,
                              hint=None if severity == ERROR else f"datawrench lint-cache lint {display} shows each one")
        yield Finding("lint-cache", "summary", INFO,
                      f"{_plural(result.stats.files, 'SQL file')} linted with this SQLFluff config, "
                      f"{flagged} with violations" + (f", {result.stats.skipped} skipped as too large"
                                                      if result.stats.skipped else ""),
                      config.display)


# --------------------------------------------------------------------------- dialect-audit

DIALECT_SEVERITY = {"error": ERROR, "warn": WARNING}
# Without a schema fixture, DuckDB binding can only say "unknown table": those are not findings.
STATIC_ONLY_CATEGORIES = frozenset({"UNKNOWN_TABLE", "BIND_ERROR"})
STATIC_ONLY_MESSAGE = "DuckDB binding supports read-only queries only"


def _source_dialect(sources: list[Source]) -> str | None:
    adapters = sorted({source.meta["adapter_type"] for source in sources
                       if source.kind == "dbt-manifest" and source.meta.get("adapter_type")})
    if len(adapters) != 1:
        return None
    return ADAPTER_DIALECTS.get(adapters[0], adapters[0])


def dialect_audit(sources: list[Source], options) -> Iterator[Finding]:
    sql = [source for source in sources if source.kind == "sql"]
    if not sql:
        return
    inferred = _source_dialect(sources)
    if options.dialect is None or not (options.dialect[0] or inferred):
        seen = f"; the dbt manifest says the source dialect is {inferred}" if inferred else ""
        example = f"{inferred or 'snowflake'}:{'snowflake' if inferred == 'duckdb' else 'duckdb'}"
        yield Finding("dialect-audit", "needs-dialect", INFO,
                      f"{_plural(len(sql), 'SQL file')} not audited for a dialect migration: no dialect pair given{seen}",
                      hint=f"datawrench check --dialect {inferred or 'FROM'}:TO, for example --dialect {example}")
        return
    read = options.dialect[0] or inferred
    write = options.dialect[1]
    need("sqlglot", "duckdb", "yaml")
    import duckdb

    from datawrench.dialect_audit.audit import run_audit
    from datawrench.dialect_audit.schema import InputError

    schema = str(options.sql_schema) if options.sql_schema else None
    displays = {str(item.path): item.display for item in sql}
    try:
        report = run_audit(list(displays), read, write, schema)
    except (InputError, OSError, duckdb.Error) as exc:
        yield Finding("dialect-audit", "invalid-config", ERROR, f"audit not run: {_short(exc)}",
                      hint="dialect names are SQLGlot's (snowflake, bigquery, postgres, tsql, duckdb, ...); "
                           "--sql-schema needs a duckdb target")
        return
    static = write == "duckdb" and schema is None
    flagged = 0
    for statement in report["statements"]:
        display = displays.get(statement["file"], statement["file"])
        kept = [item for item in statement["diagnostics"]
                if not (static and (item["category"] in STATIC_ONLY_CATEGORIES
                                    or (item["category"] == "UNSUPPORTED"
                                        and item["message"].startswith(STATIC_ONLY_MESSAGE))))]
        flagged += bool(kept)
        for item in kept:
            category = item["category"]
            location = item.get("location") or {}
            yield Finding("dialect-audit", category.lower().replace("_", "-"), DIALECT_SEVERITY[item["severity"]],
                          _short(item["message"]), display,
                          line=location.get("line_start") or statement.get("line_start"),
                          subject=f"statement {statement['index']}")
    templated = 0
    for notice in report["notices"]:
        display = displays.get(notice["file"], notice["file"])
        if notice["category"] == "JINJA_SKIPPED":
            templated += 1
        elif notice["category"] == "EMPTY_INPUT":
            yield Finding("dialect-audit", "empty-input", INFO, "no SQL statements found", display)
        else:
            yield Finding("dialect-audit", "input-skipped", WARNING, f"not audited: {_short(notice['message'])}",
                          display)
    if templated:
        yield Finding("dialect-audit", "jinja-skipped", INFO,
                      f"Jinja in {_plural(templated, 'SQL file')}: not audited",
                      hint=f"run `dbt compile`, then: datawrench check target/compiled --dialect {read}:{write}")
    summary = report["summary"]
    yield Finding("dialect-audit", "summary", INFO,
                  f"{read} -> {write}: {_plural(summary['statements'], 'statement')} in "
                  f"{_plural(summary['files'] - templated, 'file')}, {summary['statements'] - flagged} OK, "
                  f"{flagged} flagged"
                  + ("; DuckDB binding skipped (pass --sql-schema FILE to bind against empty tables)" if static else ""))


# --------------------------------------------------------------------------- polars-vet

def polars_vet(source: Source, options) -> Iterator[Finding]:
    from datawrench.polars_vet.analysis import analyze
    from datawrench.polars_vet.cli import read_source

    try:
        # Discovery never follows symlinks; resolving keeps the reader's symlink guard quiet.
        text, _, _, _ = read_source(source.path.resolve())
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        yield Finding("polars-vet", "unreadable", WARNING, f"not analysed: {_short(reason)}", source.display)
        return
    for diagnostic in analyze(text, source.display):
        if diagnostic.code == "PLV000":
            yield Finding("polars-vet", "PLV000", WARNING, f"not fully analysed: {_short(diagnostic.message)}",
                          source.display, line=diagnostic.line)
            continue
        hint = f"datawrench polars-vet check {source.display}"
        if diagnostic.edits:
            hint += " --fix rewrites it to a scan_* reader"
        yield Finding("polars-vet", diagnostic.code, WARNING, diagnostic.message, source.display,
                      line=diagnostic.line, hint=hint)


# --------------------------------------------------------------------------- parquet-unify

def parquet_unify(source: Source, options) -> Iterator[Finding]:
    need("pyarrow")
    from datawrench.parquet_unify.catalog import Catalog, scan
    from datawrench.parquet_unify.types import UnifyError

    entries = []
    for path, display in source.files:
        try:
            catalog = scan(path)
            problems = [item["error"] for item in catalog.unreadable]
        except UnifyError as exc:
            problems = [str(exc)]
        if problems:
            yield Finding("parquet-unify", "unreadable", WARNING,
                          f"cannot read the Parquet footer: {_clean(problems[0], source, (path, display))}", display,
                          hint="the file may be truncated or not Parquet; engines reading the folder will fail on it")
            continue
        entries.extend(catalog.entries)
    if len(entries) < 2:
        return  # nothing to compare
    report = Catalog(source.path.resolve(), entries, [], False).report()
    groups = len(report["groups"])
    files = len(entries)
    rank = {"conflict": 0, "widenable": 1, "missing_in_some": 2}
    drifting = sorted((c for c in report["columns"] if c["classification"] in rank or c["missing_in_some"]),
                      key=lambda c: (rank.get(c["classification"], 2), c["path"]))
    hint = (f"datawrench parquet-unify plan {source.display} --explain proposes a lossless common schema; "
            "`parquet-unify recipe` prints DuckDB or Polars readers for it")
    for column in drifting[:MAX_COLUMN_FINDINGS]:
        missing = sum(1 for value in column["files"].values() if value is None)
        absent = f"; missing in {missing} of {files} files" if missing else ""
        types = " / ".join(column["types"])
        if column["classification"] == "conflict":
            yield Finding("parquet-unify", "type-conflict", ERROR,
                          f"no lossless common type across files ({types}){absent}: {_short(column['reason'], 120)}",
                          source.display, pointer=column["path"], subject=column["path"], hint=hint)
        elif column["classification"] == "widenable":
            yield Finding("parquet-unify", "widenable", WARNING,
                          f"type differs across files ({types}); unifies losslessly to {column['unified']}{absent}",
                          source.display, pointer=column["path"], subject=column["path"], hint=hint)
        else:
            yield Finding("parquet-unify", "missing-column", WARNING,
                          f"missing in {missing} of {files} files ({types} elsewhere); a glob read may fail or "
                          "drop it", source.display, pointer=column["path"], subject=column["path"], hint=hint)
    if len(drifting) > MAX_COLUMN_FINDINGS:
        yield Finding("parquet-unify", "more-columns", INFO,
                      f"{len(drifting) - MAX_COLUMN_FINDINGS} more drifting columns not listed", source.display,
                      hint=hint)
    if groups > 1:
        yield Finding("parquet-unify", "schema-groups", INFO,
                      f"{files} files in {groups} schema groups, {report['rows']} rows (footers only)", source.display)
    else:
        yield Finding("parquet-unify", "consistent", INFO,
                      f"{files} files share one schema, {report['rows']} rows (footers only)", source.display)


# --------------------------------------------------------------------------- seedtypes

ENGINES = {"duckdb": "DuckDB", "polars": "Polars"}


def _engines(names) -> str:
    return " and ".join(ENGINES.get(name, name) for name in sorted(set(names)))


def seedtypes(source: Source, options) -> Iterator[Finding]:
    need("duckdb", "fsspec", "polars", "yaml")
    import duckdb

    from datawrench.seedtypes.csvio import SeedtypesError

    if source.kind == "csv":
        yield from _csv_defaults(source, duckdb, SeedtypesError)
        return
    from datawrench.seedtypes.check import check_project

    try:
        payload = check_project(source.path)
    except (SeedtypesError, duckdb.Error, OSError, UnicodeError) as exc:
        text = _clean(exc, source)
        yield Finding("seedtypes", "not-checked", INFO if "Jinja" in text else WARNING,
                      f"seed configuration not checked: {text}", source.display,
                      hint="seedtypes reads dbt_project.yml and seed properties statically (no Jinja, no dbt)")
        return
    lossy: dict[tuple[str, str], list[dict]] = defaultdict(list)
    failed: dict[tuple[str, str], list[str]] = defaultdict(list)
    for issue in payload["issues"]:
        if "line" in issue:
            lossy[(issue["path"], issue["column"])].append(issue)
        else:
            failed[(issue["path"], issue["engine"])].append(issue["column"])
    for (path, column), issues in sorted(lossy.items()):
        display = _child(source, path)
        lines = [issue["line"] for issue in issues if issue.get("line")]
        yield Finding("seedtypes", "unpinned-lossy", WARNING,
                      f"default {_engines(i['engine'] for i in issues)} CSV read alters values; "
                      "the seed has no column_types pin for it",
                      display, line=min(lines) if lines else None, subject=column,
                      hint=f"datawrench seedtypes emit {display} --target dbt prints column_types that keep every "
                           "value (then dbt seed --full-refresh)")
    for (path, engine), columns in sorted(failed.items()):
        display = _child(source, path)
        yield Finding("seedtypes", "reader-failed", WARNING,
                      f"the default {ENGINES.get(engine, engine)} CSV reader fails on this seed; "
                      f"{_plural(len(columns), 'column')} without a column_types pin",
                      display, hint=f"datawrench seedtypes compare {display} shows where")
    risks = len(lossy) + sum(len(columns) for columns in failed.values())
    yield Finding("seedtypes", "summary", INFO,
                  f"{_plural(payload['seeds_checked'], 'seed')} read with the DuckDB and Polars defaults; "
                  f"{_plural(risks, 'unpinned risk')}",
                  source.display)


def _csv_defaults(source: Source, duckdb, SeedtypesError) -> Iterator[Finding]:
    from datawrench.seedtypes.compare import compare

    try:
        report = compare(source.path)
    except (SeedtypesError, duckdb.Error, OSError, UnicodeError) as exc:
        yield Finding("seedtypes", "not-checked", INFO, f"not compared: {_clean(exc, source)}", source.display)
        return
    columns: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for engine, result in report["engines"].items():
        if result.get("status") == "ERROR":
            yield Finding("seedtypes", "reader-failed", INFO,
                          f"the default {ENGINES.get(engine, engine)} CSV reader fails on this file",
                          source.display, hint=f"datawrench seedtypes compare {source.display} shows where")
        for name, detail in result.get("columns", {}).items():
            if detail.get("status") == "LOSSY":
                columns[name].append((engine, detail))
    for name, details in sorted(columns.items()):
        lines = [d["first_mismatch_line"] for _, d in details if d.get("first_mismatch_line")]
        changed = max(d["mismatches"] for _, d in details)
        yield Finding("seedtypes", "lossy-default", INFO,
                      f"default {_engines(e for e, _ in details)} CSV read alters {_plural(changed, 'value')}",
                      source.display, line=min(lines) if lines else None, subject=name,
                      hint=f"datawrench seedtypes emit {source.display} --target duckdb|polars prints a reader "
                           "with pinned types")


# --------------------------------------------------------------------------- duckmate

def _duckdb_evidence(folder: Path) -> bool:
    """dbmate-style folders are common for Postgres too: parse only when DuckDB is evident."""
    for directory in (folder.parent, folder.parent.parent):
        try:
            schema = directory / "schema.sql"
            if schema.is_file() and not schema.is_symlink():
                with schema.open("rb") as stream:
                    if stream.read(256).decode("utf-8", "replace").startswith(DUCKMATE_SNAPSHOT):
                        return True
            if any(entry.suffix.lower() == ".duckdb" and entry.is_file() for entry in directory.iterdir()):
                return True
        except OSError:
            continue
    return False


def duckmate(source: Source, options) -> Iterator[Finding]:
    if not _duckdb_evidence(source.path):
        yield Finding("duckmate", "not-duckdb", INFO,
                      "dbmate-style migrations with no sign of DuckDB (no duckmate schema snapshot or .duckdb file "
                      "next to them); not parsed", source.display,
                      hint=f"if they are DuckDB migrations: datawrench duckmate --url duckdb:PATH --migrations-dir {source.display} "
                           "status")
        return
    need("duckdb")
    from datawrench.duckmate.core import Error, connect, migrations, statements

    try:
        files = migrations(source.path)
    except Error as exc:
        file = os.path.join(source.display, Path(exc.path).name) if exc.path else source.display
        located = [(Path(exc.path), file)] if exc.path else []
        yield Finding("duckmate", "invalid-migration", ERROR, _clean(exc, source, *located), file,
                      hint="duckmate reads NUMERIC_VERSION_name.sql files with one `-- migrate:up` followed by one "
                           "`-- migrate:down`")
        return
    if not files:
        return
    rejected = 0
    con = connect()
    try:
        for migration in files:
            display = os.path.join(source.display, migration.path.name)
            for section, sql in (("up", migration.up), ("down", migration.down)):
                try:
                    parsed = statements(con, sql, display, protected=MIGRATIONS_TABLE)
                except Error as exc:
                    rejected += 1
                    message = str(exc).removeprefix(f"{display}: ")
                    yield Finding("duckmate", "unsupported-statement" if exc.code == 2 else "parse-error", ERROR,
                                  _short(message), display, subject=f"migrate:{section}",
                                  hint="duckmate would stop at this migration and roll it back")
                    continue
                if section == "down" and not parsed:
                    yield Finding("duckmate", "empty-down", INFO,
                                  "the migrate:down section is empty: a rollback will not undo this migration",
                                  display)
    finally:
        con.close()
    sections = 2 * len(files)
    span = files[0].version if len(files) == 1 else f"{files[0].version}–{files[-1].version}"
    yield Finding("duckmate", "summary", INFO,
                  f"{_plural(len(files), 'migration')} ({span}): {sections - rejected} of {sections} sections "
                  "accepted by DuckDB's parser and duckmate's statement policy (nothing executed)",
                  source.display)


# --------------------------------------------------------------------------- tapcheck

# Operational stops: the input was not fully checked. Missing final STATE is a warning.
TAP_WARNINGS = frozenset({"TC008", "TC018", "TC020", "TC021", "TC022"})


def _tap_findings(report: dict, source: Source, skip: frozenset = frozenset()) -> Iterator[Finding]:
    for item in report["diagnostics"]:
        if item["code"] in skip:
            continue
        path = item.get("path")
        where = ".".join(str(part) for part in path) if path else ""
        origin = item.get("source")
        # BATCH diagnostics name the batch file relative to the message file's folder.
        display = (source.display if origin in (None, source.display, "<stdin>", "<catalog>")
                   else os.path.normpath(os.path.join(os.path.dirname(source.display), str(origin))))
        yield Finding("tapcheck", item["code"], WARNING if item["code"] in TAP_WARNINGS else ERROR,
                      _short(item["message"]) + (f" at {where}" if where else ""), display,
                      line=item.get("line") or None,
                      subject=f"stream {item['stream']}" if item.get("stream") else None,
                      pointer="/" + "/".join(str(part) for part in path) if path else None)


def tapcheck(source: Source, options) -> Iterator[Finding]:
    need("jsonschema", "referencing", "rfc3339_validator")
    from datawrench.tapcheck.engine import Validator

    if source.kind == "singer-catalog":
        report = Validator().run(io.BytesIO(b""), source=source.display, catalog=source.data,
                                 catalog_source=source.display)
        # The empty main input is ours, not the catalog's.
        problems = [d for d in report["diagnostics"] if d["code"] != "TC021"]
        yield from _tap_findings({"diagnostics": problems}, source)
        if not problems:
            yield Finding("tapcheck", "catalog", INFO,
                          f"{_plural(len(report['streams']), 'stream')} ({report['protocol']}): every schema, key and "
                          "cursor path registers", source.display)
        return
    with source.path.open("rb") as handle:
        report = Validator(batch_root=source.path.resolve().parent).run(handle, source=source.display)
    yield from _tap_findings(report, source)
    records = sum(stream["records"] for stream in report["streams"])
    yield Finding("tapcheck", "summary", INFO,
                  f"{_plural(records, 'record')} in {_plural(len(report['streams']), 'stream')} "
                  f"({report['protocol'] or 'unknown protocol'}); {_plural(report['errors'], 'contract problem')}"
                  + ("; stopped early, the rest was not checked" if report["stopped_early"] else ""),
                  source.display,
                  hint=f"datawrench tapcheck validate {source.display} --strict --check-order --check-duplicates "
                       "adds stricter contracts")


# --------------------------------------------------------------------------- singer-replay

def singer_replay(source: Source, options) -> Iterator[Finding]:
    import zlib

    from datawrench.singer_replay.cassette import verify
    from datawrench.singer_replay.common import ReplayError

    try:
        summary = verify(source.path)
    except (ReplayError, OSError, EOFError, ValueError, zlib.error) as exc:
        yield Finding("singer-replay", "invalid-cassette", ERROR, f"cassette does not verify: {_clean(exc, source)}",
                      source.display,
                      hint="tap-replay refuses it; record a new cassette with `datawrench singer-replay record -o NEW_DIR`")
        return
    records = sum(summary["counts"].values())
    yield Finding("singer-replay", "verified", INFO,
                  f"{_plural(summary['message_count'], 'message')}, {_plural(records, 'record')} in "
                  f"{_plural(len(summary['streams']), 'stream')}: checksums, catalog and counts match the manifest"
                  + ("; recorded with --limit (a prefix of the tap output)" if summary["truncated"] else ""),
                  source.display)


def missing_dependency(check: str, module: str, extra: str | None, count: int) -> Finding:
    install = f"pipx install 'datawrench[{extra}]' (or pip install 'datawrench[{extra}]')" if extra else None
    return Finding(check, "missing-dependency", INFO,
                   f"not run on {_plural(count, 'input')}: the optional dependency {module} is not installed",
                   hint=install)
