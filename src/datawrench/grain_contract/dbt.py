"""Read existing dbt artifacts; never invoke dbt or recreate its Jinja compiler."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ijson
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .compiler import ContractError


@dataclass(frozen=True)
class DbtInput:
    sql: str
    contract: dict
    model: str
    package: str
    provenance: dict


def _extra_ctes(node):
    entries = node.get("extra_ctes") or []
    if not isinstance(entries, list) or any(
        not isinstance(e, dict) or not isinstance(e.get("id"), str) for e in entries
    ):
        raise ContractError(
            "dbt extra_ctes must be an array of objects with string ids"
        )
    return entries


def _source_checksum(node, label):
    checksum = node.get("checksum")
    if (
        not isinstance(checksum, dict)
        or not isinstance(checksum.get("checksum"), str)
        or not checksum["checksum"]
        or not isinstance(checksum.get("name"), str)
        or not checksum["name"]
    ):
        raise ContractError(
            f"dbt {label} is missing its source checksum; use an actual dbt compiled manifest"
        )
    return {"name": checksum["name"], "checksum": checksum["checksum"]}


def _ephemeral_relation(entry, dialect):
    fragment = entry.get("sql") if entry else None
    if not isinstance(fragment, str) or not fragment.strip():
        raise ContractError(
            "ephemeral ref has no injected CTE SQL; run dbt compile including its consuming model"
        )
    try:
        parsed = sqlglot.parse("WITH " + fragment + " SELECT 1", read=dialect)
        if (
            len(parsed) != 1
            or not isinstance(parsed[0], exp.Select)
            or len(parsed[0].ctes) != 1
        ):
            raise ContractError(
                "dbt injected CTE SQL must contain exactly one CTE definition"
            )
        return parsed[0].ctes[0].args["alias"].this.sql(dialect=dialect)
    except (SqlglotError, ValueError, TypeError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"cannot parse dbt injected CTE: {exc}") from exc


def _validated_events(events, state):
    """Check duplicate keys without assembling the full manifest in memory."""
    stack = []
    for prefix, event, value in events:
        if not stack and not state.get("started"):
            if event != "start_map":
                raise ContractError("dbt manifest root must be an object")
            state["started"] = True
        if prefix in {"metadata", "nodes", "sources"} and event not in {
            "map_key",
            "end_map",
        }:
            if event != "start_map":
                raise ContractError(f"dbt manifest.{prefix} must be an object")
            state[prefix] = True
        if event == "start_map":
            stack.append(set())
        elif event == "start_array":
            stack.append(None)
        elif event == "map_key":
            if value in stack[-1]:
                raise ContractError(
                    f"dbt manifest: duplicate JSON key {value!r} at {prefix or '$'}"
                )
            stack[-1].add(value)
        elif event in {"end_map", "end_array"}:
            stack.pop()
        yield prefix, event, value


def read_manifest(path: str | Path, model: str, contract: dict) -> dict:
    """Select model/ref/source records with ijson, never a whole-artifact load.

    Memory is bounded by a single node, selected nodes, and object-key sets.
    metadata / sources use additional sequential passes over the local file.
    """
    if not isinstance(contract, dict) or not isinstance(
        contract.get("primary_keys"), dict
    ):
        raise ContractError("contract.primary_keys must be an object")
    refs = {model}
    source_refs = set()
    for key in contract["primary_keys"]:
        if not isinstance(key, str):
            raise ContractError("primary_keys names must be strings")
        if key.startswith("ref:"):
            refs.add(key[4:])
        elif key.startswith("source:"):
            source_refs.add(key[7:])
    path = Path(path)
    selected = {}
    state = {}
    try:
        with path.open("rb") as stream:
            events = _validated_events(ijson.parse(stream), state)
            for key, node in ijson.kvitems(events, "nodes"):
                if (
                    key in refs
                    or isinstance(node, dict)
                    and isinstance(node.get("name"), str)
                    and node["name"] in refs
                ):
                    selected[key] = node
        if not state.get("nodes"):
            raise ContractError("dbt manifest must contain a nodes object")
        # Injected ephemeral models are SQL text inside the consuming model;
        # their raw source checksums must be guarded along with that model's.
        consuming = _choose(selected, model, {"model"})
        missing = {entry["id"] for entry in _extra_ctes(consuming)} - selected.keys()
        if missing:
            with path.open("rb") as stream:
                for key, node in ijson.kvitems(stream, "nodes"):
                    if key in missing:
                        selected[key] = node
        with path.open("rb") as stream:
            metadata = next(ijson.items(stream, "metadata"), {})
        sources = {}
        if source_refs:
            with path.open("rb") as stream:
                for key, node in ijson.kvitems(stream, "sources"):
                    if (
                        key in source_refs
                        or isinstance(node, dict)
                        and isinstance(node.get("name"), str)
                        and node["name"] in source_refs
                    ):
                        sources[key] = node
        return {"metadata": metadata, "nodes": selected, "sources": sources}
    except (ijson.JSONError, UnicodeError, OverflowError) as exc:
        raise ContractError(f"{path}: invalid dbt manifest JSON: {exc}") from exc


def _choose(nodes: dict, selector: str, resource_types: set[str]) -> dict:
    matches = [
        node
        for key, node in nodes.items()
        if isinstance(node, dict)
        and isinstance(node.get("resource_type"), str)
        and node.get("resource_type") in resource_types
        and (key == selector or node.get("name") == selector)
    ]
    if not matches:
        raise ContractError(
            f"dbt manifest: no {sorted(resource_types)} matches {selector!r}"
        )
    if len(matches) != 1:
        raise ContractError(
            f"dbt manifest: {selector!r} is ambiguous; select a unique_id: {[n.get('unique_id') for n in matches]}"
        )
    if not isinstance(matches[0].get("unique_id"), str) or not matches[0]["unique_id"]:
        raise ContractError(f"dbt manifest: {selector!r} has no string unique_id")
    return matches[0]


def from_manifest(
    manifest: dict[str, Any], model: str, contract: dict[str, Any]
) -> DbtInput:
    """Resolve compiled SQL and `ref:` / `source:` PK declarations from a manifest.

    A ref value is a model/seed/snapshot name or unique_id. A source value is a
    source unique_id or a table name (the latter must be unambiguous).
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), dict):
        raise ContractError("dbt manifest must contain a nodes object")
    if not isinstance(contract, dict):
        raise ContractError("contract must be an object")
    metadata = manifest.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ContractError("dbt manifest.metadata must be an object")
    for field in ("dbt_version", "dbt_schema_version"):
        if metadata.get(field) is not None and not isinstance(metadata[field], str):
            raise ContractError(f"dbt manifest.metadata.{field} must be a string")
    node = _choose(manifest["nodes"], model, {"model"})
    # dbt's artifact schema renamed compiled_sql to compiled_code in v1.3.
    sql = node.get("compiled_code", node.get("compiled_sql"))
    if not isinstance(sql, str) or not sql.strip():
        raise ContractError(
            f"dbt model {model!r} has no compiled_code/compiled_sql; run dbt compile first"
        )
    for field in ("name", "package_name", "unique_id"):
        if not isinstance(node.get(field), str) or not node[field]:
            raise ContractError(f"dbt model is missing {field}")
    model_checksum = _source_checksum(node, "model")
    injected = {entry["id"]: entry for entry in _extra_ctes(node)}
    ephemeral_checksums = []
    for node_id in injected:
        dependency = manifest["nodes"].get(node_id)
        if not isinstance(dependency, dict):
            raise ContractError(
                f"dbt injected model {node_id} is absent from the manifest"
            )
        ephemeral_checksums.append(
            {
                "unique_id": node_id,
                "checksum": _source_checksum(dependency, "injected model"),
            }
        )
    resolved = copy.deepcopy(contract)
    pks = resolved.get("primary_keys")
    if not isinstance(pks, dict):
        raise ContractError("primary_keys must be an object")
    keys = {}
    dependencies = []
    for selector, columns in pks.items():
        if not isinstance(selector, str):
            raise ContractError("primary_keys names must be strings")
        if selector.startswith("ref:"):
            target = _choose(
                manifest["nodes"], selector[4:], {"model", "seed", "snapshot"}
            )
        elif selector.startswith("source:"):
            sources = manifest.get("sources", {})
            if not isinstance(sources, dict):
                raise ContractError("dbt manifest.sources must be an object")
            target = _choose(sources, selector[7:], {"source"})
        else:
            target = None
        if target:
            relation = target.get("relation_name")
            if not isinstance(relation, str) or not relation.strip():
                if target.get("unique_id") not in injected:
                    raise ContractError(
                        f"dbt input {selector!r} has no physical relation or injected CTE"
                    )
                relation = _ephemeral_relation(
                    injected[target["unique_id"]], contract.get("dialect", "duckdb")
                )
            dependencies.append(
                {
                    "selector": selector,
                    "unique_id": target.get("unique_id"),
                    "relation_name": relation,
                }
            )
        else:
            relation = selector
        if relation in keys:
            raise ContractError(
                f"dbt primary_keys resolve to the same relation twice: {relation}"
            )
        keys[relation] = columns
    resolved["primary_keys"] = keys
    return DbtInput(
        sql,
        resolved,
        node["name"],
        node["package_name"],
        {
            "kind": "dbt_manifest",
            "unique_id": node["unique_id"],
            "dbt_version": metadata.get("dbt_version"),
            "dbt_schema_version": metadata.get("dbt_schema_version"),
            "model_checksum": model_checksum,
            "ephemeral_checksums": ephemeral_checksums,
            "resolved_primary_keys": dependencies,
        },
    )
