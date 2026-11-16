"""Read footers and explicit string Hive partition values, never column data."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import unquote
import pyarrow as pa
import pyarrow.parquet as pq

from .types import Conflict, UnifyError, fingerprint, flatten_parts, merge_many, pointer_path, schema_spec


@dataclass
class Entry:
    path: Path
    physical: pa.Schema
    schema: pa.Schema
    rows: int
    partitions: dict
    row_groups: list[dict] | None = None


@dataclass
class Catalog:
    root: Path
    entries: list[Entry]
    unreadable: list[dict]
    hive: bool
    row_groups: bool = False

    def groups(self):
        groups = {}
        for entry in self.entries:
            groups.setdefault(fingerprint(entry.schema), []).append(entry)
        return groups

    def report(self):
        maps = [flatten_parts(e.schema) for e in self.entries]
        paths = dict.fromkeys(p for m in maps for p in m)
        columns = []
        for path in paths:
            present = [m[path] for m in maps if path in m]
            unique = list(dict.fromkeys(present))
            target, reason = None, None
            try:
                target = str(merge_many(present))
                classification = "widenable" if len(unique) > 1 else "identical"
                if len(present) < len(maps):
                    classification = "missing_in_some" if len(unique) == 1 else classification
            except UnifyError as exc:
                classification, reason = "conflict", str(exc)
            columns.append({"path": pointer_path(path), "path_parts": list(path), "classification": classification,
                            "missing_in_some": len(present) < len(maps),
                            "types": list(map(str, unique)), "unified": target, "reason": reason,
                            "files": {str(e.path): str(m[path]) if path in m else None for e, m in zip(self.entries, maps)}})
        report = {"root": str(self.root), "hive": self.hive, "files": len(self.entries),
                "rows": sum(e.rows for e in self.entries), "unreadable": self.unreadable,
                "groups": [{"fingerprint": fp, "files": [str(e.path) for e in entries],
                            "schema": schema_spec(entries[0].schema)} for fp, entries in self.groups().items()],
                "columns": columns}
        if self.row_groups:
            report["row_groups_by_file"] = [
                {"file": str(e.path), "rows": e.rows, "row_groups": e.row_groups}
                for e in sorted(self.entries, key=lambda e: str(e.path))]
        return report


def scan(path, *, hive=False, skip_unreadable=False, row_groups=False):
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise UnifyError(f"symlink input is not allowed: {raw}")
    root = raw.resolve()
    if not root.exists() or not (root.is_dir() or root.is_file()):
        raise UnifyError(f"input does not exist or is not a regular file/directory: {root}")
    # rglob does not recurse symlink directories. Report links rather than following them.
    candidates = sorted(root.rglob("*")) if root.is_dir() else [root]
    entries, unreadable = [], []
    for file in candidates:
        if file.is_dir() and not file.is_symlink():
            continue
        if file.name in ("_metadata", "_common_metadata") and not file.is_symlink():
            continue
        try:
            if file.is_symlink() or not file.is_file():
                raise UnifyError("symlinks and special files are not allowed")
            footer = pq.ParquetFile(file)
            physical = footer.schema_arrow
            if len(set(physical.names)) != len(physical.names):
                raise UnifyError("duplicate column names")
            if not len(physical):
                raise UnifyError("zero-column Parquet files are unsupported")
            partitions = {}
            if hive:
                parent = file.relative_to(root).parent if root.is_dir() else Path()
                for component in parent.parts:
                    if "=" not in component:
                        continue
                    key, value = map(unquote, component.split("=", 1))
                    if not key or key in partitions or key in physical.names:
                        raise UnifyError(f"duplicate/empty Hive key or physical column collision: {key!r}")
                    partitions[key] = None if value == "__HIVE_DEFAULT_PARTITION__" else value
            schema = pa.schema(list(physical) + [pa.field(k, pa.string()) for k in partitions])
            metadata = footer.metadata
            layout = None
            if row_groups:
                layout = []
                for index in range(metadata.num_row_groups):
                    group = metadata.row_group(index)
                    layout.append({"row_group": index, "rows": group.num_rows,
                                   "total_byte_size": group.total_byte_size})
            entries.append(Entry(file, physical, schema, metadata.num_rows, partitions, layout))
            footer.close()
        except Exception as exc:
            unreadable.append({"file": str(file), "error": str(exc)})
    catalog = Catalog(root, entries, unreadable, hive, row_groups)
    if not entries and not unreadable:
        raise UnifyError(f"empty input: {root}")
    # Return unreadable details to the CLI even when the caller must not plan/execute it.
    if not entries and skip_unreadable:
        raise UnifyError("no readable Parquet files remain after skipping unreadable files")
    return catalog


def plan(catalog, policy="widen"):
    if not catalog.entries:
        raise UnifyError("no readable Parquet files")
    if policy == "strict" and len(catalog.groups()) != 1:
        drift = [c for c in catalog.report()["columns"] if c["classification"] != "identical" or c["missing_in_some"]]
        raise Conflict("strict policy requires identical schemas; " + str(drift) + "; files=" + str([str(e.path) for e in catalog.entries]))
    names = list(dict.fromkeys(n for e in catalog.entries for n in e.schema.names))
    fields = []
    for name in names:
        types = [e.schema.field(name).type for e in catalog.entries if name in e.schema.names]
        try:
            fields.append(pa.field(name, merge_many(types, policy)))
        except UnifyError as exc:
            files = [str(e.path) for e in catalog.entries if name in e.schema.names]
            raise Conflict(f"column {name!r} in {files}: {exc}") from exc
    return pa.schema(fields)


def _order_mismatches(catalog):
    """Compare each container with the first-occurrence union of its field names.

    This order is available even when a type conflict prevents an executable
    target. Missing/null/non-struct parents contribute no child order.
    """
    containers = {}
    for entry in catalog.entries:
        orders = {(): entry.schema.names}
        orders.update({parts: [f.name for f in t] for parts, t in flatten_parts(entry.schema).items()
                       if pa.types.is_struct(t)})
        for parts, names in orders.items():
            # Duplicate names are invalid, not an order difference.
            if len(set(names)) == len(names):
                containers.setdefault(parts, []).append((entry.path, names))
    mismatches = []
    for parts, sources in containers.items():
        target = list(dict.fromkeys(n for _, names in sources for n in names))
        for file, source in sources:
            present = set(source)
            if source != [n for n in target if n in present]:
                mismatches.append({"file": str(file), "path": pointer_path(parts), "path_parts": list(parts),
                                   "reason": "field_order", "source_order": source, "target_order": target})
    return sorted(mismatches, key=lambda m: (m["path"], json.dumps(m["path_parts"], ensure_ascii=True), m["file"]))


def plan_report(catalog, policy="widen"):
    """Explain every lossless field bound, including when the selected policy fails.

    Only schema is an executable plan. Column bounds always describe lossless
    widening, independently of strict rejection or explicit stringification.
    """
    columns = catalog.report()["columns"]
    columns.sort(key=lambda c: (c["path"], json.dumps(c["path_parts"], ensure_ascii=True)))
    order_mismatches = _order_mismatches(catalog)
    try:
        target = plan(catalog, policy)
    except Conflict as exc:
        return {"policy": policy, "ok": False, "schema": None, "error": str(exc),
                "columns": columns, "order_mismatches": order_mismatches, "unreadable": catalog.unreadable}
    return {"policy": policy, "ok": True, "schema": schema_spec(target), "error": None,
            "columns": columns, "order_mismatches": order_mismatches, "unreadable": catalog.unreadable}
