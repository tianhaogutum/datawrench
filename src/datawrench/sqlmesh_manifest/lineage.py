"""Allowlisted provenance and translation gaps; no rendering or code execution."""
from __future__ import annotations

import hashlib
from typing import Any

from .exporter import serialize


def _gap(semantic: str, reason: str) -> dict[str, str]:
    return {"semantic": semantic, "reason": reason}


def build_lineage_report(context: Any, manifest: dict) -> dict:
    """Explain this exporter-generated manifest using already loaded metadata.

    Do not serialize arbitrary model/config dictionaries, SQL, exception text,
    macro bodies or audit arguments: they may contain credentials. Names and
    project-relative file paths are intentionally retained for provenance.
    """
    resources = {**manifest["nodes"], **manifest["sources"]}
    models = dict(context.models)

    def reference(uid: str) -> dict:
        node = resources[uid]
        meta = node.get("meta", {})
        return {
            "dbt_node_id": uid,
            "sqlmesh_name": meta.get("sqlmesh_name") or meta.get("sqlmesh_audit"),
            "file": None if meta.get("implicit") else node["original_file_path"],
        }

    entries = []
    for uid, node in sorted(resources.items()):
        meta = node.get("meta", {})
        origin = reference(uid)
        del origin["dbt_node_id"]
        origin.update(sqlmesh_kind=meta.get("sqlmesh_kind"),
                      sqlmesh_audit=meta.get("sqlmesh_audit"),
                      attached_node=meta.get("attached_node"))
        gaps = []
        model = models.get(meta.get("sqlmesh_name"))
        if model is not None:
            if model.cron:
                gaps.append(_gap("cron", "Schedule retained as metadata; no dbt schedule or workflow is generated."))
            kind = model.kind.name.value
            if kind.startswith(("INCREMENTAL_", "SCD_TYPE_2")):
                gaps.append(_gap("kind_execution", "Mapped to incremental metadata only; SQLMesh state, intervals and merge/SCD execution rules are not translated."))
            for field in ("pre_statements", "post_statements", "on_virtual_update"):
                if getattr(model, field, None):
                    gaps.append(_gap(field, "SQLMesh workflow statements are neither executed nor translated to dbt hooks."))
            for field in ("partitioned_by", "clustered_by", "physical_properties", "virtual_properties", "session_properties"):
                if getattr(model, field, None):
                    gaps.append(_gap(field, "SQLMesh storage/session settings are not translated to dbt adapter configuration."))
            if model.is_sql and (model.python_env or model.jinja_macros.root_macros or model.jinja_macros.packages):
                gaps.append(_gap("macro_definitions", "Macro implementations are not exported; only available rendered SQL is retained."))
        if node.get("language") == "python":
            gaps.append(_gap("python_execution", "Python model entry points are not executed or converted to dbt code; compiled_code is null."))
        elif node.get("language") == "sql" and node.get("compiled_code") is None:
            gaps.append(_gap("compiled_sql", "Offline rendering was unavailable; raw code is retained in the manifest without retrying execution."))
        if node["resource_type"] == "test":
            gaps.append(_gap("audit_execution", "Audit SQL/metadata is retained, but SQLMesh audit execution and NULL semantics are not converted to dbt test execution."))
        if meta.get("implicit"):
            gaps.append(_gap("source_declaration", "Dependency has no local SQLMesh definition; an implicit source was emitted and its original file is unknown."))
        entries.append({
            "dbt_node_id": uid, "resource_type": node["resource_type"], "origin": origin,
            "dependencies": [reference(dep) for dep in sorted(manifest["parent_map"][uid])],
            "unconverted": sorted(gaps, key=lambda gap: gap["semantic"]),
        })

    project_gaps = []
    if context.models_with_tests:
        project_gaps.append(_gap("unit_tests", "SQLMesh unit-test definitions are not converted to dbt unit_tests."))
    for field in ("before_all", "after_all"):
        if getattr(context.config, field, None):
            project_gaps.append(_gap(field, "Project workflow statements are neither executed nor exported as dbt hooks."))
    return {
        "report_version": 1,
        "project_name": manifest["metadata"]["project_name"],
        "manifest_sha256": hashlib.sha256(serialize(manifest).encode("utf-8")).hexdigest(),
        "resources": entries,
        "project_unconverted": sorted(project_gaps, key=lambda gap: gap["semantic"]),
    }
