"""Bounded, opt-in project impact overrides; every selector is exact."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .manifest import InputError, _constraints, _reject_constant, _unique_pairs, load_json_input

MAX_POLICY_BYTES = 1024 * 1024
MAX_POLICY_RULES = 1000
MAX_PATH_SEGMENTS = 65  # Type parser depth plus a terminal nullability selector.
TYPE_RULES = frozenset({
    "decimal_capacity", "integer_range", "string_capacity", "scalar_incompatible",
    "nested_type_invariant", "strict_widening", "nullability_added", "nullability_removed",
    "struct_field_added", "struct_field_removed", "struct_field_order",
})
CONSTRAINT_RULES = frozenset({"constraint_guarantee_removed", "constraint_guarantee_added"})
SELECTORS = {"field", "array_element", "map_key", "map_value", "nullability", "constraint"}
ENTRY_KEYS = {"model", "column", "path_segments", "rule", "severity"}


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _path_key(segments: list) -> str:
    return json.dumps(segments, sort_keys=True)


def _validate_path(segments: object, constraint: bool, label: str) -> None:
    if not isinstance(segments, list) or len(segments) > MAX_PATH_SEGMENTS:
        raise InputError(f"{label}.path_segments must be an array of at most 65 selectors")
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get("kind"), str) or segment["kind"] not in SELECTORS:
            raise InputError(f"{label}: unknown or malformed path selector")
        kind = segment["kind"]
        keys = {"kind", "name"} if kind == "field" else {"kind", "signature"} if kind == "constraint" else {"kind"}
        if segment.keys() != keys:
            raise InputError(f"{label}: unexpected or missing path selector attributes")
        if kind == "field" and not _nonempty(segment["name"]):
            raise InputError(f"{label}: field selector requires a nonempty name")
        if kind == "nullability" and index != len(segments) - 1:
            raise InputError(f"{label}: nullability selector must be last")
        if kind == "constraint":
            if not constraint or len(segments) != 1 or not _nonempty(segment["signature"]):
                raise InputError(f"{label}: constraint requires one exact signature selector")
            try:
                signature = json.loads(segment["signature"], object_pairs_hook=_unique_pairs,
                                       parse_constant=_reject_constant)
                _constraints([signature], label)
                semantic_keys = {"type", "expression", "columns", "to", "to_columns"}
                if (signature.keys() - semantic_keys or any(v in (None, [], "") for v in signature.values())
                        or json.dumps(signature, sort_keys=True) != segment["signature"]):
                    raise InputError(f"{label}: use the exact normalized signature from a report")
            except (ValueError, RecursionError) as e:
                raise InputError(f"{label}: invalid constraint signature; copy it from a report") from e
    if constraint and (len(segments) != 1 or segments[0]["kind"] != "constraint"):
        raise InputError(f"{label}: constraint rule requires one signature selector")


@dataclass(frozen=True)
class PolicyEntry:
    severity: str
    pointer: str


@dataclass
class ProjectPolicy:
    source: str
    entries: dict[tuple[str, str | None, str, str], PolicyEntry]

    @classmethod
    def from_dict(cls, data: object, *, source: str = "<memory>") -> ProjectPolicy:
        if not isinstance(data, dict) or data.keys() != {"version", "rules"}:
            raise InputError("policy requires exactly version and rules")
        if type(data["version"]) is not int or data["version"] != 1:
            raise InputError("policy.version must be integer 1")
        rules = data["rules"]
        if not isinstance(rules, list) or len(rules) > MAX_POLICY_RULES:
            raise InputError("policy.rules must be an array of at most 1000 entries")
        entries = {}
        for index, item in enumerate(rules):
            label = f"policy /rules/{index}"
            if not isinstance(item, dict) or item.keys() != ENTRY_KEYS:
                raise InputError(f"{label}: requires exactly model, column, path_segments, rule and severity")
            rule = item["rule"]
            if rule == "unparseable_type":
                raise InputError(f"{label}: unparseable_type cannot be overridden; unknown compatibility cannot be made safe")
            if not isinstance(rule, str) or rule not in TYPE_RULES | CONSTRAINT_RULES:
                raise InputError(f"{label}: unknown rule; use a documented known type or constraint rule")
            if not _nonempty(item["model"]) or not item["model"].startswith("model."):
                raise InputError(f"{label}: model must be an exact dbt model unique_id")
            constraint = rule in CONSTRAINT_RULES
            if not _nonempty(item["column"]) and not (constraint and item["column"] is None):
                raise InputError(f"{label}: column must be a nonempty string (or null for model constraints)")
            if not isinstance(item["severity"], str) or item["severity"] not in {"breaking", "non_breaking"}:
                raise InputError(f"{label}: severity must be breaking or non_breaking")
            _validate_path(item["path_segments"], constraint, label)
            key = (item["model"], item["column"], _path_key(item["path_segments"]), rule)
            if key in entries:
                raise InputError(f"{label}: duplicate policy selector (including conflicting severities)")
            entries[key] = PolicyEntry(item["severity"], f"/rules/{index}")
        return cls(source, entries)

    def apply(self, model: str, column: str | None, details: list[dict]) -> set[str]:
        """Return matched pointers; accounting belongs to the comparison, not this policy."""
        matched = set()
        for detail in details:
            # Defense in depth: no policy may turn parsing uncertainty into compatibility.
            if detail.get("certainty") != "known":
                continue
            key = (model, column, _path_key(detail["path_segments"]), detail["rule"])
            entry = self.entries.get(key)
            if entry is not None:
                matched.add(entry.pointer)
                detail["policy"] = {"source": self.source, "pointer": entry.pointer,
                                    "severity": entry.severity, "default_breaking": detail["breaking"]}
                detail["breaking"] = entry.severity == "breaking"
        return matched

    def unused_diagnostics(self, matched: set[str]) -> list[dict]:
        diagnostics = []
        # Dict insertion order retains policy array order, including /rules/10 after /rules/9.
        for (model, column, path, rule), entry in self.entries.items():
            if entry.pointer in matched:
                continue
            selectors = {"model": model, "column": column, "path_segments": json.loads(path), "rule": rule}
            message = (f"unused_policy_entry: {self.source}#{entry.pointer}: "
                       f"{json.dumps(selectors, sort_keys=True)} severity={entry.severity}; "
                       "no known type/constraint detail matched this exact entry in this comparison. "
                       "Check the selectors and comparison scope; unchanged or excluded contracts also leave entries unused.")
            diagnostics.append({"code": "unused_policy_entry", "source": self.source, "pointer": entry.pointer,
                                **selectors, "severity": entry.severity, "message": message})
        return diagnostics


def load_policy(path: str | Path) -> ProjectPolicy:
    data = load_json_input(path, label="policy", max_bytes=MAX_POLICY_BYTES, limit="1 MiB")
    return ProjectPolicy.from_dict(data, source=str(path))
