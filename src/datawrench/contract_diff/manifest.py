"""Read bounded dbt manifest artifacts without importing or running dbt."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import stat
from pathlib import Path
from typing import Any

MAX_MANIFEST_BYTES = 64 * 1024 * 1024
CONSTRAINT_TYPES = {"not_null", "primary_key", "unique", "foreign_key", "check"}
CONSTRAINT_KEYS = {"type", "name", "expression", "columns", "to", "to_columns", "warn_unenforced", "warn_unsupported"}


class InputError(ValueError):
    pass


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} must be a nonempty string")
    return value


def _version(value: Any, label: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))
                              or (isinstance(value, float) and not math.isfinite(value))
                              or (isinstance(value, str) and not value)):
        raise InputError(f"{label} must be a string or number")


def _constraints(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise InputError(f"{label} must be an array")
    for item in value:
        item = _object(item, label + " item")
        if not isinstance(item.get("type"), str) or item["type"] not in CONSTRAINT_TYPES:
            raise InputError(f"{label}: unsupported constraint type; supported: {', '.join(sorted(CONSTRAINT_TYPES))}")
        if item.keys() - CONSTRAINT_KEYS:
            raise InputError(f"{label}: unsupported constraint attributes")
        for key in ("name", "expression", "to"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise InputError(f"{label}.{key} must be a string or null")
        for key in ("columns", "to_columns"):
            if key in item:
                if not isinstance(item[key], list) or not all(isinstance(x, str) and x for x in item[key]):
                    raise InputError(f"{label}.{key} must be an array of nonempty strings")


def _validate_model(node: dict, key: str) -> None:
    if node.get("unique_id", key) != key:
        raise InputError("model unique_id does not match its nodes key")
    _text(node.get("name"), f"{key}.name")
    if "package_name" in node:
        _text(node["package_name"], f"{key}.package_name")
    config = _object(node.get("config", {}), f"{key}.config")
    if "enabled" in config and not isinstance(config["enabled"], bool):
        raise InputError(f"{key}.config.enabled must be boolean")
    for container, label in ((node, key), (config, f"{key}.config")):
        if "contract" in container:
            contract = _object(container["contract"], label + ".contract")
            if "enforced" in contract and not isinstance(contract["enforced"], bool):
                raise InputError(f"{label}.contract.enforced must be boolean")
    if "contract" in node and "contract" in config:
        a, b = node["contract"].get("enforced"), config["contract"].get("enforced")
        if a is not None and b is not None and a != b:
            raise InputError(f"{key}: conflicting node/config contract enforcement")
    for field in ("version", "latest_version"):
        _version(node.get(field), f"{key}.{field}")
    columns = _object(node.get("columns", {}), f"{key}.columns")
    for name, column in columns.items():
        _text(name, f"{key} column name")
        column = _object(column, f"{key}.{name}")
        if column.get("name", name) != name:
            raise InputError(f"{key}: column name does not match columns key")
        dtype = column.get("data_type")
        if dtype is not None and not isinstance(dtype, str):
            raise InputError(f"{key}.{name}.data_type must be string or null")
        _constraints(column.get("constraints", []), f"{key}.{name}.constraints")
    _constraints(node.get("constraints", []), f"{key}.constraints")


def enforced(node: dict) -> bool:
    return node.get("contract", {}).get("enforced", node.get("config", {}).get("contract", {}).get("enforced", False))


def group(node: dict, key: str) -> tuple[str, str]:
    return node.get("package_name", key.split(".")[1] if "." in key else ""), node["name"]


@dataclass
class Manifest:
    metadata: dict
    models: dict[str, dict]
    disabled: set[str]

    @classmethod
    def from_dict(cls, data: dict) -> Manifest:
        data = _object(data, "manifest")
        nodes = _object(data.get("nodes"), "manifest.nodes")
        metadata = _object(data.get("metadata", {}), "manifest.metadata")
        for field in ("adapter_type", "project_name", "project_id", "dbt_schema_version"):
            if metadata.get(field) is not None and not isinstance(metadata[field], str):
                raise InputError(f"metadata.{field} must be string or null")
        models, disabled = {}, set()
        for key, raw in nodes.items():
            node = _object(raw, f"nodes.{key}")
            _text(node.get("resource_type"), f"nodes.{key}.resource_type")
            if node.get("resource_type") != "model":
                continue
            _validate_model(node, key)
            if not node.get("config", {}).get("enabled", True):
                disabled.add(key)
            else:
                models[key] = node
        disabled_data = _object(data.get("disabled", {}), "manifest.disabled")
        for key, entries in disabled_data.items():
            if not isinstance(entries, list) or not all(isinstance(x, dict) for x in entries):
                raise InputError("disabled values must be arrays of node objects")
            if any(x.get("resource_type") == "model" for x in entries):
                if key in models:
                    raise InputError("model appears in both active and disabled nodes")
                disabled.add(key)
        return cls(metadata, models, disabled)


def _unique_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise InputError("non-finite JSON number")


def load_json_input(path: str | Path, *, label: str, max_bytes: int, limit: str) -> Any:
    """Read a bounded regular JSON file with duplicate/non-finite rejection."""
    try:
        # Nonblocking open prevents a FIFO/device input from hanging CI.
        fd = os.open(Path(path), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise InputError(f"{label} input must be a regular file")
            if info.st_size > max_bytes:
                raise InputError(f"{label} exceeds {limit} limit")
            raw = f.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise InputError(f"{label} exceeds {limit} limit")
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs,
                          parse_constant=_reject_constant)
        return data
    except InputError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as e:
        # File paths and content can contain terminal escapes or secrets. Keep errors concise.
        raise InputError(f"cannot read valid {label} ({type(e).__name__})") from e


def load_manifest(path: str | Path) -> Manifest:
    return Manifest.from_dict(load_json_input(path, label="manifest",
                                              max_bytes=MAX_MANIFEST_BYTES, limit="64 MiB"))
