"""Translate loaded SQLMesh models, audits and logical DAG into dbt v12."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlglot import exp
from sqlmesh.core import dialect as d
from sqlmesh.core.audit.builtin import BUILT_IN_AUDITS

from .loading import ExportError, contained

SCHEMA_URL = "https://schemas.getdbt.com/dbt/manifest/v12.json"
EPOCH = "1970-01-01T00:00:00Z"
RENDER_TIME = dict(start="1970-01-01", end="1970-01-01", execution_time="1970-01-01")


def materialization(kind: str) -> str:
    if kind in ("FULL", "VIEW", "EMBEDDED", "SEED"):
        return {"FULL": "table", "VIEW": "view", "EMBEDDED": "ephemeral", "SEED": "seed"}[kind]
    if kind.startswith("INCREMENTAL_") or kind.startswith("SCD_TYPE_2"):
        return "incremental"
    raise ExportError(f"Unsupported SQLMesh model kind: {kind}")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identifier(name: str, identity: str, *, disambiguate: bool = False) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]", "_", name) or "unnamed"
    if clean != name or disambiguate:
        clean += "_" + _hash(identity)[:12]
    return clean


def _sql(value: Any, dialect: str) -> str:
    return value.sql(dialect=dialect) if isinstance(value, exp.Expr) else str(value)


def _raw(node: Any, root: Path) -> tuple[str, str]:
    path = getattr(node, "_path", None)
    if path is None:
        return "external_models.yaml", ""
    path = contained(Path(path), root)
    return path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")


def _compiled(render: Callable, dialect: str, label: str, warnings: list[str]) -> str | None:
    try:
        query = render()
        if query is not None:
            return query.sql(dialect=dialect, pretty=True)
    except Exception as exc:
        # Do not echo exception text: config / macro exceptions can contain secrets.
        warnings.append(f"{label}: offline rendering unavailable ({type(exc).__name__}); compiled_code=null")
        return None
    warnings.append(f"{label}: offline rendering unavailable; compiled_code=null")
    return None


def _base(uid: str, resource: str, name: str, package: str, table: exp.Table,
          path: str, raw: str) -> dict:
    return {
        "database": table.catalog or None, "schema": table.db or "default",
        "name": name, "resource_type": resource, "package_name": package,
        "path": path, "original_file_path": path, "unique_id": uid,
        "fqn": [package, name], "alias": table.name,
        "checksum": {"name": "sha256", "checksum": _hash(raw)},
        "raw_code": raw,
    }


def _generic(audit: Any, args: dict, model: Any) -> list[tuple[str | None, dict | None]]:
    """Only claim dbt generic semantics for genuine built-ins and literal args.

    Multi-column SQLMesh not_null/unique_values check columns independently, so
    expand to one generic dbt test per column. Custom overrides stay singular.
    """
    if audit is not BUILT_IN_AUDITS.get(audit.name):
        return [(None, None)]
    dialect = model.dialect
    if audit.name in ("not_null", "unique_values") and set(args) <= {"columns", "condition", "blocking"}:
        columns = args.get("columns")
        if isinstance(columns, exp.Paren):
            columns = columns.this
        columns = columns.expressions if isinstance(columns, (exp.Tuple, exp.Array)) else [columns]
        if columns and all(isinstance(c, exp.Column) for c in columns):
            return [(c.name, {"name": "not_null" if audit.name == "not_null" else "unique",
                              "kwargs": {"column_name": c.sql(dialect=dialect)}, "namespace": None}) for c in columns]
    if audit.name == "accepted_values" and set(args) <= {"column", "is_in", "condition", "blocking"}:
        column, values = args.get("column"), args.get("is_in")
        if isinstance(values, exp.Paren):
            values = values.this
        items = values.expressions if isinstance(values, (exp.Tuple, exp.Array)) else [values]
        if isinstance(column, exp.Column) and items and all(isinstance(v, exp.Literal) for v in items):
            # quote=False preserves numbers as SQL literals, strings use dbt quoting.
            if all(v.is_string for v in items) or all(not v.is_string for v in items):
                vals = [v.this if v.is_string else json.loads(v.this) for v in items]
                return [(column.name, {"name": "accepted_values", "namespace": None,
                                      "kwargs": {"column_name": column.sql(dialect=dialect),
                                                 "values": vals, "quote": items[0].is_string}})]
    return [(None, None)]


def build_manifest(context: Any, *, project_name: str | None = None,
                   deterministic: bool = False) -> tuple[dict, list[str]]:
    root = Path(context.path).resolve()
    project = project_name or context.config.project or root.name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", project):
        raise ExportError("Project name must match [A-Za-z_][A-Za-z0-9_]*; use --project-name")
    warnings: list[str] = []
    if context.metrics:
        raise ExportError("SQLMesh metrics cannot be mapped to dbt metrics in manifest v12")
    if context.models_with_tests:
        warnings.append("SQLMesh unit-test definitions are not exported as dbt unit_tests")
    models = dict(context.models)
    tables = {fqn: exp.to_table(fqn, dialect=m.dialect) for fqn, m in models.items()}
    model_names = Counter(t.name for fqn, t in tables.items() if models[fqn].kind.name.value != "EXTERNAL")
    ids: dict[str, str] = {}
    names: dict[str, str] = {}
    nodes: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    parents: dict[str, list[str]] = {}
    resource_kinds: dict[str, str] = {}

    def add_source(fqn: str, model: Any = None) -> str:
        if fqn in ids:
            return ids[fqn]
        dialect = model.dialect if model else context.config.dialect
        table = exp.to_table(fqn, dialect=dialect)
        source_identity = table.catalog + "." + table.db
        source_name = _identifier(table.db or "external", source_identity,
                                  disambiguate=bool(table.catalog and table.catalog != context.default_catalog))
        name = _identifier(table.name, fqn)
        uid = f"source.{project}.{source_name}.{name}"
        if uid in sources:
            raise ExportError("Source identifiers collide after normalization")
        path, raw = _raw(model, root) if model else ("external_models.yaml", "")
        meta = {"sqlmesh_name": fqn, "sqlmesh_kind": "EXTERNAL", "implicit": model is None}
        if model:
            meta.update(owner=model.owner, cron=model.cron,
                        grain=[_sql(g, model.dialect) for g in model.grains], tags=sorted(model.tags))
        sources[uid] = {
            "database": table.catalog or None, "schema": table.db or "default", "name": name,
            "resource_type": "source", "package_name": project, "path": path,
            "original_file_path": path, "unique_id": uid, "fqn": [project, source_name, name],
            "source_name": source_name, "source_description": "", "loader": "sqlmesh-dbt-manifest",
            "identifier": table.name, "description": model.description or "" if model else "",
            "columns": _columns(model) if model else {}, "meta": meta,
            "tags": sorted(model.tags) if model else [], "config": {"enabled": True, "meta": meta},
        }
        ids[fqn] = uid
        parents[uid] = []
        if model is None:
            warnings.append(f"{uid}: undeclared dependency exported as an implicit source")
        return uid

    for fqn, model in sorted(models.items()):
        kind = model.kind.name.value
        if kind == "EXTERNAL":
            add_source(fqn, model)
            continue
        resource = "seed" if kind == "SEED" else "model"
        name = _identifier(tables[fqn].name, fqn, disambiguate=model_names[tables[fqn].name] > 1)
        uid = f"{resource}.{project}.{name}"
        if uid in nodes:
            raise ExportError("Model identifiers collide after normalization")
        ids[fqn], names[fqn], resource_kinds[fqn] = uid, name, resource
        path, raw = _raw(model, root)
        kind_expression = model.kind.to_expression()
        if kind == "SEED":
            for prop in kind_expression.expressions:
                if prop.name.lower() == "path":
                    seed_relative = contained(model.seed_path, root).relative_to(root).as_posix()
                    prop.set("value", exp.Literal.string(seed_relative))
        meta = {
            "sqlmesh_name": fqn, "sqlmesh_kind": kind, "owner": model.owner,
            "cron": model.cron, "grain": [_sql(g, model.dialect) for g in model.grains],
            "tags": sorted(model.tags), "exporter": "sqlmesh-dbt-manifest/0.1.0",
            "render_reference_time": EPOCH, "sqlmesh_kind_definition": kind_expression.sql(dialect=model.dialect),
        }
        node = _base(uid, resource, name, project, tables[fqn], path, raw)
        node.update(description=model.description or "", columns=_columns(model), tags=sorted(model.tags), meta=meta,
                    config={"materialized": materialization(kind), "enabled": model.enabled,
                            "meta": meta, "tags": sorted(model.tags)})
        if resource == "seed":
            # dbt Seed depends_on supports macros only; DAG parents remain empty.
            seed_path = contained(model.seed_path, root)
            node["checksum"] = {"name": "sha256", "checksum": hashlib.sha256(seed_path.read_bytes()).hexdigest()}
            node["depends_on"] = {"macros": []}
            node["meta"]["seed_file"] = seed_path.relative_to(root).as_posix()
        else:
            node["language"] = "sql" if model.is_sql else "python"
            node["compiled_code"] = _compiled(lambda: model.render_query(**RENDER_TIME), model.dialect, uid, warnings) if model.is_sql else None
            if not model.is_sql:
                warnings.append(f"{uid}: Python execution is unsupported; compiled_code=null")
        nodes[uid] = node

    def resolve(fqn: str) -> str:
        return ids.get(fqn) or add_source(fqn)

    for fqn, model in sorted(models.items()):
        if fqn not in resource_kinds:
            continue
        uid = ids[fqn]
        deps = sorted(resolve(dep) for dep in context.dag.graph.get(fqn, set()))
        parents[uid] = deps
        if resource_kinds[fqn] == "model":
            nodes[uid]["depends_on"] = {"nodes": deps, "macros": []}

    for fqn, model in sorted(models.items()):
        for index, (audit, args) in enumerate(model.audits_with_args):
            variants = _generic(audit, args, model)
            for column, generic in variants:
                merged = {**audit.defaults, **args}
                if generic and column is not None and audit.name in ("not_null", "unique_values"):
                    merged["columns"] = exp.Tuple(expressions=[exp.column(column, quoted=True)])
                signature = json.dumps({k: _sql(v, model.dialect) for k, v in sorted(merged.items())}, sort_keys=True)
                name = _identifier(f"{audit.name}_{names.get(fqn, tables[fqn].name)}", fqn) + "_" + _hash(f"{fqn}:{index}:{column}:{signature}")[:12]
                uid = f"test.{project}.{name}"
                path, raw = _raw(audit, root) if audit._path else _raw(model, root)
                if not audit._path:
                    raw = audit.query.sql(dialect=model.dialect)
                node = _base(uid, "test", name, project, tables[fqn], path, raw)
                node["alias"] = name
                compiled = _compiled(lambda: model.render_audit_query(audit, **RENDER_TIME, **merged), model.dialect, uid, warnings)
                dependencies = {ids[fqn]}
                if compiled:
                    query = d.parse_one(compiled, dialect=model.dialect)
                    dependencies.update(resolve(dep) for dep in d.find_tables(query, default_catalog=context.default_catalog, dialect=model.dialect))
                deps = sorted(dependencies)
                meta = {"sqlmesh_audit": audit.name, "sqlmesh_args": json.loads(signature),
                        "attached_node": ids[fqn], "sqlmesh_blocking": audit.blocking,
                        "sqlmesh_skip": audit.skip, "render_reference_time": EPOCH}
                blocking = args.get("blocking")
                blocking = blocking == exp.true() if blocking is not None else audit.blocking
                meta["sqlmesh_blocking"] = blocking
                config = {"materialized": "test", "enabled": not audit.skip,
                          "severity": "ERROR" if blocking else "WARN", "meta": meta}
                if "condition" in merged:
                    config["where"] = _sql(merged["condition"], model.dialect)
                node.update(config=config, meta=meta, compiled_code=compiled, language="sql",
                            depends_on={"nodes": deps, "macros": []})
                if generic:
                    generic["kwargs"]["model"] = ids[fqn]
                    node.update(test_metadata=generic, attached_node=ids[fqn], column_name=column)
                nodes[uid], parents[uid] = node, deps

    for fqn, audit in sorted(context.standalone_audits.items()):
        name = _identifier(audit.name, fqn)
        uid = f"test.{project}.{name}"
        if uid in nodes:
            raise ExportError("Standalone audit identifier collides with another test")
        path, raw = _raw(audit, root)
        node = _base(uid, "test", name, project, exp.to_table(audit.name), path, raw)
        compiled = _compiled(lambda: audit.render_audit_query(**RENDER_TIME), audit.dialect, uid, warnings)
        deps = sorted(resolve(dep) for dep in context.dag.graph.get(fqn, audit.depends_on))
        node.update(config={"materialized": "test", "enabled": not audit.skip, "severity": "WARN"},
                    compiled_code=compiled, language="sql", depends_on={"nodes": deps, "macros": []},
                    meta={"sqlmesh_audit": audit.name, "standalone": True})
        nodes[uid], parents[uid] = node, deps

    children: dict[str, list[str]] = {uid: [] for uid in parents}
    for uid, dependencies in parents.items():
        for dep in dependencies:
            children[dep].append(uid)
    now = EPOCH if deterministic else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {key: {} for key in ("macros", "docs", "exposures", "metrics", "groups", "selectors", "disabled", "group_map", "saved_queries", "semantic_models", "unit_tests")}
    manifest.update(
        metadata={"dbt_schema_version": SCHEMA_URL, "dbt_version": "1.8.0",
                  "generated_at": now, "invocation_id": str(uuid.UUID(int=0) if deterministic else uuid.uuid4()),
                  "project_name": project, "project_id": _hash(project), "adapter_type": context.connection_config.DIALECT,
                  "send_anonymous_usage_stats": False, "env": {}},
        nodes=nodes, sources=sources, parent_map=parents,
        child_map={k: sorted(v) for k, v in children.items()},
    )
    return manifest, sorted(warnings)


def _columns(model: Any) -> dict:
    types = model.columns_to_types or {}
    descriptions = model.column_descriptions
    return {name: {"name": name, "description": descriptions.get(name, ""),
                   "data_type": types[name].sql(dialect=model.dialect) if name in types else None,
                   "meta": {}, "tags": []}
            for name in sorted(types.keys() | descriptions.keys())}


def serialize(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
