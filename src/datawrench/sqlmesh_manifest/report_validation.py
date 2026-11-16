"""Offline consistency checks for export provenance reports, without a project."""
from __future__ import annotations

import hashlib
import sys
from collections import Counter
from pathlib import Path

from jsonschema import Draft7Validator

from .exporter import SCHEMA_URL
from .validation import ManifestError, read_json_artifact, read_json_stream, validate_manifest


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_TEXT = {"type": "string", "minLength": 1}
_OPTIONAL_TEXT = {"type": ["string", "null"]}
_REFERENCE = {"dbt_node_id": _TEXT, "file": _OPTIONAL_TEXT, "sqlmesh_name": _OPTIONAL_TEXT}
_GAPS = {"type": "array", "items": _object({"semantic": _TEXT, "reason": _TEXT})}
_REPORT_SCHEMA = _object({
    "report_version": {"type": "integer", "const": 1},
    "project_name": _TEXT,
    "manifest_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "resources": {"type": "array", "items": _object({
        "dbt_node_id": _TEXT,
        "resource_type": {"enum": ["model", "seed", "source", "test"]},
        "origin": _object({
            "file": _OPTIONAL_TEXT, "sqlmesh_name": _OPTIONAL_TEXT,
            "sqlmesh_kind": _OPTIONAL_TEXT, "sqlmesh_audit": _OPTIONAL_TEXT,
            "attached_node": _OPTIONAL_TEXT,
        }),
        "dependencies": {"type": "array", "items": _object(_REFERENCE)},
        "unconverted": _GAPS,
    })},
    "project_unconverted": _GAPS,
})
_REPORT_VALIDATOR = Draft7Validator(_REPORT_SCHEMA)


class ReportValidationError(ManifestError):
    """A fixed diagnostic separate from the existing human-readable message."""

    def __init__(self, message: str, *, input: str, path: str, rule: str):
        super().__init__(message)
        self.diagnostic = {"input": input, "path": path, "rule": rule}


def validation_result(*, counts: dict | None = None, diagnostic: dict | None = None) -> dict:
    """Versioned result with no partial metadata on failure."""
    valid = diagnostic is None
    return {"validation_version": 1, "valid": valid,
            "report_version": 1 if valid else None,
            "counts": counts if valid else None,
            "diagnostics": [] if valid else [diagnostic]}


def _read_input(path: str | Path, kind: str) -> tuple[dict, bytes]:
    try:
        # Select stdin before Path normalization, and only for the report.
        # The manifest always remains a file, including a literal dash path.
        if kind == "report" and path == "-":
            try:
                return read_json_stream(sys.stdin.buffer, kind)
            except ManifestError:
                raise
            except (AttributeError, OSError, UnicodeError, ValueError) as exc:
                raise ManifestError(f"Cannot read report ({type(exc).__name__})") from None
        return read_json_artifact(path, kind)
    except ManifestError as exc:
        raise ReportValidationError(str(exc), input=kind, path="$", rule="read_json") from None


def validate_report(report_path: str | Path, manifest_path: str | Path) -> dict:
    """Return resource/edge counts, or raise ManifestError with a safe diagnostic.

    Only the exact report string '-' selects bounded binary stdin; Path('-')
    and all manifest arguments remain files. The hash binds exact manifest
    bytes, not a normalized reserialization. This is consistency checking,
    not authentication or reconstruction of SQLMesh gaps.
    """
    report, _ = _read_input(report_path, "report")
    manifest, content = _read_input(manifest_path, "manifest")
    if type(report.get("report_version")) is not int or report["report_version"] != 1:
        raise ReportValidationError("$.report_version: supported report version is integer 1",
                                    input="report", path="$.report_version", rule="report_version")
    # One shape error is enough; avoid values, unexpected keys and exception
    # text in diagnostics. All schema paths below consist of fixed field names
    # or array indexes because arbitrary object properties are not permitted.
    error = next(_REPORT_VALIDATOR.iter_errors(report), None)
    if error is not None:
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path)
        raise ReportValidationError(f"{path}: report schema rule '{error.validator}' failed",
                                    input="report", path=path, rule=f"schema.{error.validator}")
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("dbt_schema_version") != SCHEMA_URL:
        raise ReportValidationError("Report validation requires a dbt manifest v12",
                                    input="manifest", path="$.metadata.dbt_schema_version", rule="manifest_version")
    try:
        validate_manifest(manifest)
    except ManifestError as exc:
        # Manifest dictionaries can contain arbitrary user keys in error paths.
        # Preserve text compatibility, but never copy those paths into JSON.
        raise ReportValidationError(str(exc), input="manifest", path="$", rule="manifest_validation") from None
    if report["manifest_sha256"] != hashlib.sha256(content).hexdigest():
        raise ReportValidationError("$.manifest_sha256: does not match supplied manifest bytes",
                                        input='pair', path='$.manifest_sha256', rule='manifest_sha256')
    if report["project_name"] != manifest["metadata"].get("project_name"):
        raise ReportValidationError("$.project_name: differs from manifest",
                                        input='pair', path='$.project_name', rule='project_name')

    resources = {**manifest["nodes"], **manifest["sources"]}
    if manifest["nodes"].keys() & manifest["sources"].keys():
        raise ReportValidationError("Manifest resource IDs are not unique across nodes and sources",
                                        input='manifest', path='$', rule='resource_ids')
    ids = [entry["dbt_node_id"] for entry in report["resources"]]
    if len(ids) != len(set(ids)):
        raise ReportValidationError("$.resources: duplicate resource IDs",
                                        input='report', path='$.resources', rule='duplicate_resources')
    if set(ids) != resources.keys():
        raise ReportValidationError("$.resources: IDs must cover manifest nodes and sources exactly",
                                        input='pair', path='$.resources', rule='resources')

    def reference(uid: str) -> dict:
        node = resources[uid]
        meta = node.get("meta") or {}
        return {
            "dbt_node_id": uid,
            "sqlmesh_name": meta.get("sqlmesh_name") or meta.get("sqlmesh_audit"),
            "file": None if meta.get("implicit") else node["original_file_path"],
        }

    # Sources/seeds have no depends_on.nodes in dbt v12. Retain their edges
    # from graph indexes, including when only the child index is non-null.
    parents = manifest.get("parent_map")
    if parents is None:
        parents = {uid: [] for uid in resources}
        for uid, children in (manifest.get("child_map") or {}).items():
            for child in children:
                if child in parents:
                    parents[child].append(uid)

    for index, entry in enumerate(report["resources"]):
        path = f"$.resources[{index}]"
        uid = entry["dbt_node_id"]
        node = resources[uid]
        if entry["resource_type"] != node["resource_type"]:
            raise ReportValidationError(f"{path}.resource_type: differs from manifest",
                                        input='pair', path=f'{path}.resource_type', rule='resource_type')
        meta = node.get("meta") or {}
        origin = reference(uid)
        del origin["dbt_node_id"]
        origin.update(sqlmesh_kind=meta.get("sqlmesh_kind"),
                      sqlmesh_audit=meta.get("sqlmesh_audit"),
                      attached_node=meta.get("attached_node"))
        attachment = origin["attached_node"]
        if attachment is not None and (not isinstance(attachment, str) or attachment not in resources):
            raise ReportValidationError(f"{path}.origin.attached_node: dangling manifest attachment",
                                        input='manifest', path=f'{path}.origin.attached_node', rule='attachment')
        if node.get("attached_node") is not None and node["attached_node"] != attachment:
            raise ReportValidationError(f"{path}.origin.attached_node: inconsistent manifest attachment",
                                        input='manifest', path=f'{path}.origin.attached_node', rule='attachment')
        if entry["origin"] != origin:
            raise ReportValidationError(f"{path}.origin: differs from manifest provenance",
                                        input='pair', path=f'{path}.origin', rule='origin')
        deps = [dep["dbt_node_id"] for dep in entry["dependencies"]]
        if len(deps) != len(set(deps)):
            raise ReportValidationError(f"{path}.dependencies: duplicate dependency IDs",
                                        input='report', path=f'{path}.dependencies', rule='duplicate_dependencies')
        declared = node.get("depends_on", {}).get("nodes")
        expected = set(declared if declared is not None else parents.get(uid, []))
        if expected - resources.keys():
            raise ReportValidationError(f"{path}.dependencies: manifest edge refers outside nodes and sources",
                                        input='manifest', path=f'{path}.dependencies', rule='dependencies')
        if set(deps) != expected:
            raise ReportValidationError(f"{path}.dependencies: IDs differ from manifest edges",
                                        input='pair', path=f'{path}.dependencies', rule='dependencies')
        for dep_index, dep in enumerate(entry["dependencies"]):
            if dep != reference(dep["dbt_node_id"]):
                raise ReportValidationError(f"{path}.dependencies[{dep_index}]: differs from manifest provenance",
                                        input='pair', path=f'{path}.dependencies[{dep_index}]', rule='dependency_provenance')

    counts = Counter(entry["resource_type"] for entry in report["resources"])
    return {"resources": len(report["resources"]), "models": counts["model"],
            "seeds": counts["seed"], "sources": counts["source"], "tests": counts["test"],
            "dependencies": sum(len(entry["dependencies"]) for entry in report["resources"])}
