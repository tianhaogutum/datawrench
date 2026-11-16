"""Canonical, secret-free fingerprints of engine and template inputs."""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlfluff.core import FluffConfig
from jinja2 import TemplateSyntaxError, nodes

SCHEMA = 1
MAX_TREE_FILES = 100_000
MAX_TREE_BYTES = 256 * 1024 * 1024


class InputError(ValueError):
    """An unsupported or unsafe input; never silently fall back to success."""


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InputError("Unsupported non-JSON configuration/cache value") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def config_dump(config: FluffConfig) -> dict:
    # iter_vals() is a display interface: it drops None and private derived lists.
    # The pinned API's underlying mapping preserves these distinctions. Exclude
    # only the two runtime objects, whose identity/repr is not deterministic.
    data = config._configs
    values = {key: ({k: v for k, v in value.items()
                     if k not in ("dialect_obj", "templater_obj")}
                    if key == "core" else value) for key, value in data.items()}
    # Macro sections may define the same Jinja macro under different config
    # names: SQLFluff's last definition wins. Sorting JSON keys alone would
    # erase a semantically relevant reordering of those definitions.
    order = []
    def walk(mapping: dict, path: list[str]) -> None:
        order.append([path, list(mapping)])
        for key, value in mapping.items():
            if isinstance(value, dict):
                walk(value, [*path, key])
    walk(values, [])
    return {"values": values, "mapping_order": order}


def engine_stamp() -> dict:
    packages = sorted((d.metadata["Name"], d.version) for d in metadata.distributions())
    plugins = sorted((ep.name, ep.value, d.metadata["Name"], d.version)
                     for d in metadata.distributions() for ep in d.entry_points
                     if ep.group == "sqlfluff")
    return {"schema": SCHEMA, "sqlfluff": metadata.version("sqlfluff"),
            "python": sys.version, "packages": packages, "plugins": plugins}


def dependency_stamp(stamp: dict) -> dict:
    """Attribute SQLFluff's own distribution/built-ins only to its version."""
    return {**{k: v for k, v in stamp.items() if k not in ("sqlfluff", "packages", "plugins")},
            "packages": [[name, None if name.lower() == "sqlfluff" else version]
                         for name, version in stamp["packages"]],
            "plugins": [[*p[:-1], None] if len(p) == 4 and p[2].lower() == "sqlfluff" else p
                        for p in stamp["plugins"]]}


@dataclass(frozen=True)
class TemplateInputs:
    fingerprint: str
    dependencies: str
    cacheable: bool
    notice: str | None = None
    template_files: str = digest([])
    environment: str = digest([])


class Fingerprinter:
    """Hash dependency trees once per invocation; inputs must be stable during it."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir.absolute()
        self.trees: dict[tuple[str, bool], str] = {}
        self.jinja_candidates: list[str] = []

    def tree(self, path: Path, *, project: bool = False) -> str:
        path = path.absolute()
        for component in (path, *path.parents):
            if component.is_symlink():
                raise InputError(f"Template dependency symlink is unsupported: {component}")
        token = (str(path), project)
        if token in self.trees:
            return self.trees[token]
        records = []
        count = size = 0

        def visit(item: Path) -> None:
            nonlocal count, size
            if project and (item == self.cache_dir or item.name in
                            {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}):
                return
            # No following dependency symlinks: that would hide unbounded inputs
            # outside the declared tree, cycles, and possible special files.
            if item.is_symlink():
                raise InputError(f"Template dependency symlink is unsupported: {item}")
            info = item.stat()
            rel = str(item.relative_to(path))
            if stat.S_ISDIR(info.st_mode):
                records.append([rel, "directory"])
                for child in sorted(item.iterdir()):
                    visit(child)
            elif stat.S_ISREG(info.st_mode):
                count += 1
                size += info.st_size
                if count > MAX_TREE_FILES or size > MAX_TREE_BYTES:
                    raise InputError(f"Template inputs exceed 100000 files / 256 MiB: {path}")
                content = item.read_bytes()
                records.append([rel, hashlib.sha256(content).hexdigest()])
                if b"random" in content:
                    self.jinja_candidates.append(content.decode("utf-8", errors="replace"))
            else:
                raise InputError(f"Template dependency is not a regular file/directory: {item}")

        if path.exists() or path.is_symlink():
            visit(path)
        else:
            records.append([".", "missing"])
        result = digest([str(path), records])
        self.trees[token] = result
        return result

    def nondeterministic_jinja(self, config: FluffConfig, raw: str, templater: Any) -> bool:
        """Jinja's built-in random filter can change diagnostics without file edits.

        Also conservatively bypass literal 'random' arguments, e.g. map('random').
        Syntax errors are left to SQLFluff so TMP errors remain cacheable.
        """
        macros = config.get_section(("templater", "jinja", "macros")) or {}
        candidates = [raw, *macros.values(), *self.jinja_candidates]
        environment = None
        for source in candidates:
            if not isinstance(source, str) or "random" not in source:
                continue
            if environment is None:
                environment = templater._get_jinja_env(config)
            try:
                tree = environment.parse(source)
            except TemplateSyntaxError:
                continue
            if any(node.name == "random" for node in tree.find_all(nodes.Filter)) or any(
                    node.value == "random" for node in tree.find_all(nodes.Const)):
                return True
        return False

    def templater(self, config: FluffConfig, name: str, *, trust_code: bool,
                  dbt_project_hash: bool) -> tuple[str, bool, str | None]:
        result = self.template_inputs(config, name, trust_code=trust_code,
                                      dbt_project_hash=dbt_project_hash)
        return result.fingerprint, result.cacheable, result.notice

    def template_inputs(self, config: FluffConfig, name: str, *, trust_code: bool,
                        dbt_project_hash: bool) -> TemplateInputs:
        if name not in {"raw", "jinja", "python", "placeholder", "dbt"}:
            raise InputError(f"Unsupported templater: {name}")
        if name == "raw":
            return TemplateInputs(digest(None), digest([]), True)
        section = config.get_section(("templater", name)) or {}
        inputs: list = [name, section]

        def result(cacheable: bool, notice: str | None = None) -> TemplateInputs:
            # Retain the original aggregate/key serialization. Detail hashes are
            # auxiliary history only, never a new source of cache validity.
            files = [item for item in inputs[2:] if item[0] != "environment"]
            environment = [item for item in inputs[2:] if item[0] == "environment"]
            return TemplateInputs(digest(inputs), digest(inputs[2:]), cacheable, notice,
                                  digest(files), digest(environment))

        library = config.get("library_path") or section.get("library_path")
        if library and str(library).lower() != "none" and not trust_code:
            raise InputError("Python library_path imports executable code; pass "
                             "--trust-template-code only for trusted, deterministic libraries")
        for key in ("load_macros_from_path", "exclude_macros_from_path", "loader_search_path"):
            for part in str(section.get(key) or "").split(","):
                if part.strip():
                    inputs.append([key, self.tree(Path(part.strip()))])
        if library and str(library).lower() != "none":
            inputs.append(["library_path", self.tree(Path(str(library)))])
            inputs.append(["environment", digest(dict(os.environ))])
        if name == "dbt":
            notice = "dbt templater: cache bypassed (opt in with --dbt-project-hash)"
            if not trust_code:
                raise InputError(notice + "; dbt can execute code/connect to databases; "
                                 "--trust-template-code is required")
            if not dbt_project_hash:
                return result(False, notice)
            project_dir = Path(section.get("project_dir") or os.getcwd()).absolute()
            if not (project_dir / "dbt_project.yml").is_file():
                raise InputError(f"dbt project has no dbt_project.yml: {project_dir}")
            inputs.append(["project", self.tree(project_dir, project=True)])
            profiles = Path(section.get("profiles_dir") or os.environ.get("DBT_PROFILES_DIR")
                            or Path.home() / ".dbt")
            inputs.append(["profiles", self.tree(profiles)])
            inputs.append(["environment", digest(dict(os.environ))])
        # Config values already have a separate settings fingerprint. Keep the
        # original key intact, but explain only tree/environment edits as deps.
        return result(True)
