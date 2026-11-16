"""Offline format and graph validation, with bounded, value-free diagnostics."""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import BinaryIO

from dbt_artifacts_parser.parser import parse_manifest
from jsonschema import Draft7Validator
from pydantic import ValidationError

from .exporter import SCHEMA_URL

MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MANIFEST_VERSIONS = {f"https://schemas.getdbt.com/dbt/manifest/v{v}.json": v for v in range(1, 13)}
_RESOURCE_GROUPS = ("nodes", "sources", "macros", "docs", "exposures", "metrics", "groups",
                    "saved_queries", "semantic_models", "unit_tests")


class ManifestError(ValueError):
    """Keep human text separate from fixed, value-free machine diagnostics."""

    def __init__(self, message: str, *, rule: str = "internal_error", path: str = "$"):
        super().__init__(message)
        self.diagnostic = {"path": path, "rule": rule}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("Duplicate JSON object key")
        result[key] = value
    return result


def read_manifest(path: str | Path) -> dict:
    return read_json_artifact(path, "manifest")[0]


def read_manifest_input(path: str | Path) -> dict:
    """Standalone manifest input; only the literal string '-' selects stdin."""
    # Preserve './-' before Path normalization and keep read_manifest file-only.
    if path != "-":
        return read_manifest(path)
    try:
        return read_json_stream(sys.stdin.buffer, "manifest")[0]
    except ManifestError:
        raise
    except (AttributeError, OSError, UnicodeError, ValueError) as exc:
        raise ManifestError(f"Cannot read manifest ({type(exc).__name__})") from None


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ManifestError("Non-finite JSON number")
    return number


def read_json_artifact(path: str | Path, kind: str) -> tuple[dict, bytes]:
    """Read once so checksum consumers validate the same bytes they parsed."""
    try:
        with Path(path).open("rb") as stream:
            return read_json_stream(stream, kind)
    except (OSError, UnicodeError) as exc:
        raise ManifestError(f"Cannot read {kind} ({type(exc).__name__})") from None


def read_json_stream(stream: BinaryIO, kind: str) -> tuple[dict, bytes]:
    """Read bounded binary input without closing the caller's stream."""
    try:
        content = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            raise ManifestError(f"{kind.capitalize()} exceeds the 64 MiB input limit")
        data = json.loads(content, object_pairs_hook=_unique_object, parse_float=_finite_float,
                          parse_constant=lambda _: (_ for _ in ()).throw(ManifestError("Non-finite JSON number")))
    except (OSError, UnicodeError) as exc:
        raise ManifestError(f"Cannot read {kind} ({type(exc).__name__})") from None
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ManifestError(f"Invalid or truncated JSON {kind}") from None
    if not isinstance(data, dict):
        raise ManifestError(f"{kind.capitalize()} must be a JSON object")
    return data, content


@lru_cache(maxsize=1)
def schema_validator() -> Draft7Validator:
    schema = json.loads(files("datawrench.sqlmesh_manifest").joinpath("manifest-v12.json").read_text())
    return Draft7Validator(schema)


def validate_manifest(manifest: dict) -> None:
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("dbt_schema_version"), str):
        raise ManifestError("$.metadata.dbt_schema_version: required schema URL",
                            path="$.metadata.dbt_schema_version", rule="manifest_version")
    if metadata["dbt_schema_version"] == SCHEMA_URL:
        problems = []
        for error in schema_validator().iter_errors(manifest):
            path = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path)
            problems.append(f"{path}: schema rule '{error.validator}' failed")
            if len(problems) == 10:
                break
        if problems:
            raise ManifestError("\n".join(problems), rule="schema_validation")
    try:
        parse_manifest(manifest)
    except ValidationError as exc:
        errors = ["$." + ".".join(map(str, e["loc"])) + ": " + e["type"] for e in exc.errors(include_input=False)[:10]]
        raise ManifestError("\n".join(errors), rule="parser_validation") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        supported = metadata["dbt_schema_version"] in _MANIFEST_VERSIONS
        raise ManifestError("Unsupported or invalid dbt manifest schema (parser supports v1–v12)",
                            rule="parser_validation" if supported else "manifest_version",
                            path="$" if supported else "$.metadata.dbt_schema_version") from None
    try:
        _check_graph(manifest)
    except ManifestError as exc:
        raise ManifestError(str(exc), rule="graph_validation") from None


def _check_graph(manifest: dict) -> None:
    resources = {}
    for group in ("nodes", "sources", "exposures", "metrics", "semantic_models", "saved_queries", "unit_tests"):
        resources.update(manifest.get(group) or {})
    for uid, node in resources.items():
        if node.get("unique_id") != uid:
            raise ManifestError("Resource key does not match its unique_id")
    declared = {}
    for uid, node in resources.items():
        deps = node.get("depends_on", {}).get("nodes")
        if deps is not None:
            if len(deps) != len(set(deps)):
                raise ManifestError("depends_on.nodes contains duplicate edges")
            if any(dep not in resources for dep in deps):
                raise ManifestError("depends_on.nodes contains a dangling dependency")
            declared[uid] = set(deps)

    parents, children = manifest.get("parent_map"), manifest.get("child_map")
    for name, index in (("parent_map", parents), ("child_map", children)):
        if not isinstance(index, dict):
            continue  # Some manifest versions permit absent/null graph indexes.
        for uid, edges in index.items():
            if uid not in resources or any(edge not in resources for edge in edges):
                raise ManifestError(f"{name} contains a dangling resource")
            if len(edges) != len(set(edges)):
                raise ManifestError(f"{name} contains duplicate edges")

    if isinstance(parents, dict):
        for uid, deps in declared.items():
            if deps != set(parents.get(uid, [])):
                raise ManifestError("depends_on.nodes differs from parent_map")
    if isinstance(children, dict):
        incoming = {uid: set() for uid in resources}
        for uid, downstream in children.items():
            for child in downstream:
                incoming[child].add(uid)
        for uid, deps in declared.items():
            if deps != incoming[uid]:
                raise ManifestError("depends_on.nodes differs from child_map")

    if isinstance(parents, dict) and isinstance(children, dict):
        if set(parents) != set(children):
            raise ManifestError("parent_map and child_map have different keys")
        for uid, deps in parents.items():
            if any(dep not in children or uid not in children[dep] for dep in deps):
                raise ManifestError("parent_map contains a non-reciprocal edge")
        for uid, downstream in children.items():
            if any(child not in parents or uid not in parents[child] for child in downstream):
                raise ManifestError("child_map contains a non-reciprocal edge")


def manifest_validation_result(path: str | Path) -> dict:
    """Run the same standalone validation, returning one bounded JSON-safe result.

    Never derive diagnostics from exception strings or arbitrary resource keys.
    Counts deliberately mirror summary's enabled resources and parent-map edges.
    """
    result = {"manifest_validation_version": 1, "valid": False,
              "manifest_version": None, "counts": None, "diagnostics": []}
    reading = True
    try:
        manifest = read_manifest_input(path)
        reading = False
        validate_manifest(manifest)
        resources = [node for group in _RESOURCE_GROUPS for node in (manifest.get(group) or {}).values()]
        counts = Counter(node.get("resource_type") for node in resources)
        result.update(valid=True,
                      manifest_version=_MANIFEST_VERSIONS[manifest["metadata"]["dbt_schema_version"]],
                      counts={"resources": len(resources), "models": counts["model"],
                              "seeds": counts["seed"], "sources": counts["source"], "tests": counts["test"],
                              "edges": sum(len(edges) for edges in (manifest.get("parent_map") or {}).values())})
    except ManifestError as exc:
        result["diagnostics"] = [{"path": "$", "rule": "read_json"} if reading else exc.diagnostic]
    except Exception:
        result["diagnostics"] = [{"path": "$", "rule": "internal_error"}]
    return result
