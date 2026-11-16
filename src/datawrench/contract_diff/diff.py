"""Manifest contract compatibility policy and structured reports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections.abc import Iterable
import json

from .manifest import InputError, Manifest, enforced, group
from .policy import ProjectPolicy
from .types import DIALECTS, TypeParseError, compare_types, parse_type


@dataclass
class Change:
    model: str
    kind: str
    breaking: bool
    message: str
    column: str | None = None
    before: object = None
    after: object = None
    details: list[dict] = field(default_factory=list)
    version: str | None = None


@dataclass
class Report:
    dialect: str
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    equivalents: list[dict] = field(default_factory=list)
    models_compared: int = 0
    policy_diagnostics: list[dict] = field(default_factory=list)
    selected_models: list[str] | None = None
    excluded_models: list[str] | None = None

    @property
    def breaking_count(self) -> int:
        return sum(c.breaking for c in self.changes)

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.selected_models is None:
            del data["selected_models"]
        if self.excluded_models is None:
            del data["excluded_models"]
        return {"schema_version": 1, **data, "summary": {
            "models_compared": self.models_compared, "changes": len(self.changes),
            "breaking": self.breaking_count, "non_breaking": len(self.changes) - self.breaking_count,
            "equivalents": len(self.equivalents), "warnings": len(self.warnings)}}

    def exit_code(self, fail_on: str) -> int:
        return int((fail_on == "breaking" and self.breaking_count > 0) or (fail_on == "any" and bool(self.changes)))


def _constraint_map(values: list[dict]) -> dict[str, dict]:
    result = {}
    for c in values:
        # Constraint names and adapter warning switches do not change guarantees.
        c = {k: v for k, v in c.items() if k in {"type", "expression", "columns", "to", "to_columns"} and v not in (None, [], "")}
        result[json.dumps(c, sort_keys=True)] = c
    return result


def _model_added_detail(key: str) -> dict:
    return {
        "path": "model",
        # Root relative to the exact Change.model key, including literal dots/quotes.
        "path_segments": [],
        "classification": "model_added",
        "before": None,
        "after": key,
        "breaking": False,
        "rule": "model_added",
        "reason": "Adding this model to the active manifest introduces its declared model contract "
                  "at this exact unique_id; additions are non-breaking under this metadata policy. "
                  "This compares declarations only; SQL, data and warehouse enforcement behavior "
                  "are not validated. Added metadata does not establish warehouse creation.",
        "remediation": "Validate the declared contract, producing SQL, data and warehouse enforcement "
                       "behavior separately before relying on the new model.",
        # Known declaration addition, not evidence of execution or warehouse creation.
        "certainty": "known",
    }


def _model_removed_detail(key: str) -> dict:
    return {
        "path": "model",
        # Root relative to the exact Change.model key; dots are not path separators.
        "path_segments": [],
        "classification": "model_removed",
        "before": key,
        "after": None,
        "breaking": True,
        "rule": "model_removed",
        "reason": "Removing this model from the active manifest loses its declared model contract "
                  "at this exact unique_id; consumers can no longer rely on that contract in the manifest. "
                  "This compares declarations only; SQL, data and warehouse state are not validated.",
        "remediation": "Restore the original model and its declared contract, or keep the original "
                       "contract available while publishing a new model version and migrating consumers.",
        # Known manifest-level loss, not evidence of warehouse deletion or data validation.
        "certainty": "known",
    }


def _model_disabled_detail(key: str) -> dict:
    return {
        "path": "model",
        # Root relative to the exact Change.model key, including literal dots/quotes.
        "path_segments": [],
        "classification": "model_disabled",
        "before": key,
        "after": None,
        "breaking": True,
        "rule": "model_disabled",
        "reason": "Disabling this model removes its declared model contract from the active manifest "
                  "at this exact unique_id; consumers can no longer rely on that active declaration. "
                  "This compares declarations only; SQL, data and warehouse state are not validated. "
                  "Disabling metadata does not establish warehouse deletion.",
        "remediation": "Re-enable the original model and restore its declared contract, or keep the original "
                       "contract available while publishing a new model version and migrating consumers.",
        # Known active-declaration loss for either recognized disabled representation.
        "certainty": "known",
    }


def _model_identity_detail(key: str, old: dict, new: dict) -> dict:
    def identity(node: dict) -> dict:
        package, name = group(node, key)
        # Use the same effective family as detection, including the package fallback.
        return {"unique_id": key, "package_name": package, "name": name, "version": node.get("version")}

    return {
        "path": "model",
        # Root relative to the exact Change.model key, including literal dots/quotes.
        "path_segments": [],
        "classification": "model_identity_changed",
        "before": identity(old),
        "after": identity(new),
        "breaking": True,
        "rule": "model_identity_changed",
        "reason": "Changing the effective package/name family or version under the same unique_id "
                  "changes the declared model contract's original identity; this is breaking under "
                  "this metadata policy. This compares declarations only; SQL, data and warehouse "
                  "enforcement behavior are not validated. Identity metadata does not establish "
                  "warehouse renaming, creation or deletion.",
        "remediation": "Preserve the original contract under its original identity; publish a separate "
                       "model version while keeping that original contract available to consumers. "
                       "Validate SQL, data and warehouse enforcement separately.",
        # Known identity metadata change, not a migration exemption or engine guarantee.
        "certainty": "known",
    }


def _latest_version_detail(family: tuple[str, str], before: object, after: object) -> dict:
    package, name = family
    return {
        "path": "model",
        # Family metadata at the representative model root; never a sibling path.
        "path_segments": [],
        "classification": "latest_version_changed",
        "before": {"package_name": package, "name": name, "latest_version": before},
        "after": {"package_name": package, "name": name, "latest_version": after},
        "breaking": False,
        "rule": "latest_version_changed",
        "reason": "Changing the effective family's declared latest_version metadata is non-breaking "
                  "under this metadata policy. This compares declarations only; it does not establish "
                  "consumer migration or warehouse behavior. SQL, data and engine enforcement "
                  "are not validated; existing contract changes remain separate findings.",
        "remediation": "Review consumers of this family's latest-version declaration and validate "
                       "their version selection, SQL, data and warehouse enforcement separately "
                       "before relying on the changed metadata.",
        # Known consumed metadata change, not a migration or engine guarantee.
        "certainty": "known",
    }


def _column_removed_detail(before: str | None) -> dict:
    return {
        "path": "column",
        # Root of Change.model / Change.column: never split a literal column name on dots.
        "path_segments": [],
        "classification": "column_removed",
        # Retain raw metadata: recognizing a lost name does not require parsing its type.
        "before": before,
        "after": None,
        "before_type": before,
        "after_type": None,
        "breaking": True,
        "rule": "column_removed",
        "reason": "Removing this column loses its name and declared type from the contract; "
                  "consumers that reference the column can no longer rely on it. "
                  "A probable rename does not preserve the original name.",
        "remediation": "Restore the original column and its contract, or keep the original model "
                       "available while publishing a new model version and migrating consumers.",
        "certainty": "known",
    }


def _column_added_detail(after: str | None) -> dict:
    return {
        "path": "column",
        # Relative to the exact Change.model / Change.column keys, not a nested field.
        "path_segments": [],
        "classification": "column_added",
        "before": None,
        "after": after,
        "before_type": None,
        "after_type": after,
        "breaking": False,
        "rule": "column_added",
        "reason": "Adding this column extends the declared contract; additions are non-breaking "
                  "under this metadata policy. The raw declared type is retained; "
                  "type validity, SQL and data are not validated.",
        "remediation": "Validate the declared type, producing SQL and data in your warehouse "
                       "before relying on the new column.",
        # Recognizes the added name, regardless of whether its type can be parsed.
        "certainty": "known",
    }


def _enforcement_removed_detail() -> dict:
    return {
        "path": "contract",
        # Effective model contract root, for either node/config declarations.
        "path_segments": [],
        "classification": "enforcement_removed",
        "before": True,
        "after": False,
        "breaking": True,
        "rule": "enforcement_removed",
        "reason": "Turning off the enforcement declaration removes the model's declared contract "
                  "enforcement; consumers can no longer rely on that declaration. "
                  "SQL, data and warehouse enforcement behavior are not validated.",
        "remediation": "Restore the enforcement declaration, or keep the original enforced contract "
                       "available while publishing a new model version and migrating consumers. "
                       "Validate SQL, data and warehouse enforcement separately.",
        # Known declaration loss, not a claim about execution or engine guarantees.
        "certainty": "known",
    }


def _enforcement_added_detail() -> dict:
    return {
        "path": "contract",
        # Effective model contract root, for either node/config declarations.
        "path_segments": [],
        "classification": "enforcement_added",
        "before": False,
        "after": True,
        "breaking": False,
        "rule": "enforcement_added",
        "reason": "Turning on the enforcement declaration adds declared model contract enforcement; "
                  "this addition is non-breaking under this metadata policy. "
                  "SQL, data and warehouse enforcement behavior are not validated.",
        "remediation": "Validate the model SQL, existing data and warehouse enforcement behavior "
                       "separately before relying on the enforcement declaration.",
        # Known declaration addition, not a claim about execution or engine guarantees.
        "certainty": "known",
    }


def _constraint_detail(constraint: dict, signature: str, removed: bool) -> dict:
    guarantee = {
        "not_null": "null values are excluded",
        "unique": "the declared column combination is unique",
        "primary_key": "the declared primary-key guarantee is retained",
        "foreign_key": "the declared ordered reference mapping is retained",
        "check": "the exact declared check expression is retained",
    }[constraint["type"]]
    return {
        "path": "constraints",
        # Relative to Change.model / Change.column; signature identifies the exact guarantee.
        "path_segments": [{"kind": "constraint", "signature": signature}],
        "classification": "constraint_removed" if removed else "constraint_added",
        "before": constraint if removed else None,
        "after": None if removed else constraint,
        "breaking": removed,
        "rule": "constraint_guarantee_removed" if removed else "constraint_guarantee_added",
        "reason": (f"Removing this constraint loses the guarantee that {guarantee}. "
                   "Changed expressions or ordered mappings count as removal plus addition; "
                   "SQL equivalence is not inferred." if removed else
                   f"Adding this constraint declares the guarantee that {guarantee}; "
                   "additions are non-breaking under this metadata policy. Data satisfaction is not checked."),
        "remediation": ("Restore the exact original guarantee, or publish a new model version and migrate "
                        "consumers that depend on it." if removed else
                        "Validate existing data against the new guarantee before enforcing it in the warehouse."),
        "certainty": "known",
    }


def compare_manifests(base: Manifest, head: Manifest, *, dialect: str = "auto", include_unenforced: bool = False,
                      strict_widening: bool = False, policy: ProjectPolicy | None = None,
                      selected_models: Iterable[str] | None = None,
                      excluded_models: Iterable[str] | None = None) -> Report:
    warnings = []
    for key in ("project_id", "project_name", "adapter_type"):
        left, right = base.metadata.get(key), head.metadata.get(key)
        if left is not None and right is not None and left != right:
            warnings.append(f"manifests have different {key}: {left!r} -> {right!r}")
    if dialect == "auto":
        adapter = head.metadata.get("adapter_type") or base.metadata.get("adapter_type")
        # dbt uses 'spark' for some Databricks projects.
        adapter = {"spark": "databricks"}.get(adapter, adapter)
        if adapter in DIALECTS:
            dialect = adapter
        else:
            dialect = "duckdb"
            warnings.append("adapter missing or unsupported; auto uses duckdb (override with --dialect)")
    if dialect not in DIALECTS:
        raise ValueError("unsupported dialect")
    # Manifest loading validates all consumed fields before the caller reaches here.
    # Family consistency is also artifact-wide, even for an empty requested scope.
    def latest(models: dict[str, dict]) -> dict:
        result = {}
        for key, node in sorted(models.items()):
            if node.get("latest_version") is not None:
                family = group(node, key)
                value = node["latest_version"]
                if family in result and result[family][1] != value:
                    raise InputError("inconsistent latest_version within a model family")
                result[family] = (key, value)
        return result

    selected = None if selected_models is None else sorted(set(selected_models))
    excluded = None if excluded_models is None else sorted(set(excluded_models))
    scoped = selected is not None or excluded is not None
    if scoped:
        latest(base.models)
        latest(head.models)
        known = base.models.keys() | head.models.keys() | base.disabled | head.disabled
        for label, keys in (("selected", selected), ("excluded", excluded)):
            unknown = set(keys or []) - known
            if unknown:
                raise InputError(f"unknown {label} model unique_id(s): " + ", ".join(sorted(unknown)))
    report = Report(dialect, warnings=warnings, selected_models=selected, excluded_models=excluded)
    matched_policy_entries: set[str] = set()

    def finish() -> Report:
        if policy is not None:
            report.policy_diagnostics = policy.unused_diagnostics(matched_policy_entries)
            report.warnings.extend(d["message"] for d in report.policy_diagnostics)
        return report

    active = {k for k in base.models.keys() | head.models.keys()
              if include_unenforced or enforced(base.models.get(k, {})) or enforced(head.models.get(k, {}))}
    if selected is not None:
        active.intersection_update(selected)
    if excluded is not None:
        active.difference_update(excluded)
    if not active:
        scope = " in selected scope" if selected is not None else ""
        if excluded is not None:
            scope += " after exclusions"
        report.notices.append(f"No enforced contracts found{scope}; no models compared. Use --include-unenforced to inspect other models.")
        return finish()

    def add(key: str, kind: str, breaking: bool, message: str, column: str | None = None,
            before: object = None, after: object = None, details: list[dict] | None = None) -> None:
        if details and policy is not None:
            matched_policy_entries.update(policy.apply(key, column, details))
            breaking = any(d["breaking"] for d in details)
        old = base.models.get(key, {})
        version = old.get("version")
        if breaking and version is not None:
            message += f"; breaking change without version bump (version {version})"
        report.changes.append(Change(key, kind, breaking, message, column, before, after, details or [],
                                     str(version) if version is not None else None))

    def constraints(key: str, left: list, right: list, column: str | None = None) -> None:
        a, b = _constraint_map(left), _constraint_map(right)
        for sig in sorted(a.keys() - b.keys()):
            add(key, "constraint_removed", True, f"{a[sig]['type']} constraint removed", column, a[sig],
                details=[_constraint_detail(a[sig], sig, True)])
        for sig in sorted(b.keys() - a.keys()):
            add(key, "constraint_added", False, f"{b[sig]['type']} constraint added", column, after=b[sig],
                details=[_constraint_detail(b[sig], sig, False)])

    def check_added_type(key: str, name: str, column: dict) -> None:
        try:
            parse_type(column.get("data_type"), dialect)
        except TypeParseError as e:
            report.warnings.append(f"{key} column {name}: added data_type unparseable: {e}; no previous type to compare")

    for key in sorted(active):
        old, new = base.models.get(key), head.models.get(key)
        report.models_compared += 1
        if old is None:
            add(key, "model_added", False, "model/version added", after=new.get("version"),
                details=[_model_added_detail(key)])
            for name, column in sorted(new.get("columns", {}).items()):
                check_added_type(key, name, column)
            continue
        if new is None:
            disabled = key in head.disabled
            add(key, "model_disabled" if disabled else "model_removed", True,
                "model disabled" if disabled else "model removed",
                details=[_model_disabled_detail(key) if disabled else _model_removed_detail(key)])
            continue
        if old.get("version") != new.get("version") or group(old, key) != group(new, key):
            add(key, "model_identity_changed", True, "model identity/version changed under the same unique_id",
                before=old.get("version"), after=new.get("version"), details=[_model_identity_detail(key, old, new)])
        if enforced(old) and not enforced(new):
            add(key, "enforcement_removed", True, "contract enforcement turned off",
                details=[_enforcement_removed_detail()])
        elif not enforced(old) and enforced(new):
            add(key, "enforcement_added", False, "contract enforcement turned on",
                details=[_enforcement_added_detail()])
        left, right = old.get("columns", {}), new.get("columns", {})
        removed, added = left.keys() - right.keys(), right.keys() - left.keys()
        for name in sorted(removed):
            before = left[name].get("data_type")
            add(key, "column_removed", True, "column removed", name, before=before,
                details=[_column_removed_detail(before)])
        for name in sorted(added):
            check_added_type(key, name, right[name])
            after = right[name].get("data_type")
            add(key, "column_added", False, "column added", name, after=after,
                details=[_column_added_detail(after)])
        for name in sorted(left.keys() & right.keys()):
            before, after = left[name].get("data_type"), right[name].get("data_type")
            result = compare_types(before, after, dialect, strict_widening=strict_widening)
            report.warnings.extend(f"{key} column {name}: {w}" for w in result.warnings)
            if result.classification == "identical":
                if before != after:
                    report.equivalents.append({"model": key, "column": name, "before": before, "after": after})
            else:
                add(key, "type_" + result.classification, result.breaking,
                    f"{before or '<missing>'} -> {after or '<missing>'} ({result.classification})", name, before, after,
                    result.to_dict()["details"])
            constraints(key, left[name].get("constraints", []), right[name].get("constraints", []), name)
        constraints(key, old.get("constraints", []), new.get("constraints", []))
        # A hint is deliberately not a rename exemption; consumers still lose a name.
        def rename_candidates(names: set, columns: dict) -> dict:
            candidates = {}
            for name in sorted(names):
                try:
                    dtype = parse_type(columns[name].get("data_type"), dialect)
                except TypeParseError:
                    continue
                candidates.setdefault(dtype, []).append(name)
            return candidates

        removed_types, added_types = rename_candidates(removed, left), rename_candidates(added, right)
        for dtype, old_names in removed_types.items():
            new_names = added_types.get(dtype, [])
            if len(old_names) == len(new_names) == 1:
                report.notices.append(f"{key}: probable rename {old_names[0]!r} -> {new_names[0]!r} (same type); removal remains breaking")

    # latest_version is repeated on each versioned node. Report once per family.
    # Opt-in scope uses only remaining eligible records, never an excluded sibling.
    a = latest({k: v for k, v in base.models.items() if k in active} if scoped else base.models)
    b = latest({k: v for k, v in head.models.items() if k in active} if scoped else head.models)
    active_families = {group(base.models.get(k) or head.models[k], k) for k in active}
    for family in sorted((a.keys() | b.keys()) & active_families):
        old_value = a.get(family, (None, None))[1]
        new_key = b.get(family, a.get(family))[0]
        new_value = b.get(family, (None, None))[1]
        if old_value != new_value:
            add(new_key, "latest_version_changed", False, f"latest_version changed: {old_value} -> {new_value}",
                before=old_value, after=new_value, details=[_latest_version_detail(family, old_value, new_value)])
    return finish()
