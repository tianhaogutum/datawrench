"""Project loading policy: local metadata only; no engine or remote state."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from ruamel.yaml import YAML
from sqlglot import exp
from sqlmesh import Context
from sqlmesh.core import constants, dialect as d
from sqlmesh.core.config import Config
from sqlmesh.core.config.loader import load_config_from_python_module
from sqlmesh.core.loader import SqlMeshLoader
from sqlmesh.core import analytics
from sqlmesh.core.analytics.collector import AnalyticsCollector
from sqlmesh.core.analytics.dispatcher import NoopEventDispatcher


class ExportError(ValueError):
    """A project is invalid or outside the supported export boundary."""


class OfflineContext(Context):
    """Block lazy adapters, including warehouse introspection during loading."""

    @property
    def engine_adapter(self):
        raise ExportError("Warehouse access is disabled during metadata export")

    @property
    def engine_adapters(self):
        raise ExportError("Warehouse access is disabled during metadata export")

    @property
    def default_catalog_per_gateway(self):
        catalogs = {}
        for name in set(self.config.gateways) | {self.selected_gateway}:
            connection = self.config.get_connection(name)
            catalogs[name] = connection.get_catalog()
        return catalogs

    @property
    def state_sync(self):
        raise ExportError("Remote state access is disabled during metadata export")


def contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ExportError("Project input path escapes the project directory")
    return resolved


def _check_sql(path: Path, root: Path, dialect: str, trusted: bool) -> None:
    text = path.read_text(encoding="utf-8")
    if not trusted and any(token in text for token in ("{{", "{%", "{#", "JINJA_")):
        raise ExportError("Jinja templates require --trust-project")
    for statement in d.parse(text, default_dialect=dialect):
        if statement is None:
            continue
        if not trusted and statement.find(d.MacroFunc, d.PythonCode):
            raise ExportError("SQL macro functions require --trust-project")
        # SEED reads files while loading, so check before handing it to Context.
        for kind in statement.find_all(d.ModelKind):
            if kind.name.upper() == "SEED":
                for prop in kind.expressions:
                    if prop.name.lower() == "path":
                        value = prop.args.get("value")
                        if not isinstance(value, exp.Literal) or not value.is_string:
                            raise ExportError("SEED path must be a literal local path")
                        seed = value.this
                        seed_path = root / seed[7:] if seed.startswith("$root/") else path.parent / seed
                        contained(seed_path, root)


def _config(root: Path, trusted: bool) -> Config:
    paths = [root / name for name in ("config.yaml", "config.yml", "config.py") if (root / name).is_file()]
    if len(paths) != 1:
        raise ExportError("Expected exactly one SQLMesh config.yaml, config.yml or config.py")
    path = contained(paths[0], root)
    if path.suffix == ".py":
        if not trusted:
            raise ExportError("Python config requires --trust-project")
        config = load_config_from_python_module(Config, path, config_name="config")
    else:
        text = path.read_text(encoding="utf-8")
        if any(token in text for token in ("{{", "{%", "{#")):
            raise ExportError("Templated YAML config is unsupported; use literal YAML or trusted config.py")
        data = YAML(typ="safe").load(text)
        if not isinstance(data, dict):
            raise ExportError("SQLMesh config must be a YAML mapping")
        if data.get("env_vars"):
            raise ExportError("Config env_vars are unsupported; export does not modify the environment")
        config = Config(**data)
    if config.loader is not SqlMeshLoader or config.dbt:
        raise ExportError("Only native SQLMesh projects with the standard loader are supported")
    if config.loader_kwargs:
        raise ExportError("Custom loader_kwargs are unsupported")
    if not config.model_defaults.dialect:
        raise ExportError("Set model_defaults.dialect in the project config")
    if config.model_defaults.kind and config.model_defaults.kind.name.value == "SEED":
        raise ExportError("Default SEED kind is unsupported; declare the seed path in each MODEL")
    if not trusted:
        for value in (config.before_all or []) + (config.after_all or []):
            if any(t in value for t in ("@", "{{", "{%")):
                raise ExportError("Executable environment statements require --trust-project")
        for field in ("pre_statements", "post_statements", "on_virtual_update", "physical_properties", "virtual_properties", "session_properties", "kind", "audits"):
            value = getattr(config.model_defaults, field)
            if value and any(t in str(value) for t in ("@", "{{", "{%")):
                raise ExportError("Executable model defaults require --trust-project")
    return config


@contextmanager
def load_project(project: str | Path, gateway: str | None = None, *, trusted: bool = False):
    """Yield a Context without loading .env, personal config, adapters or state.

    Explicitly trusted project Python and macros are executable user code, not
    sandboxed. The exporter itself never invokes a Python model's entry point.
    """
    root = Path(project).resolve()
    if not root.is_dir():
        raise ExportError("Project directory does not exist")
    analytics.disable_analytics()
    # SQLMesh otherwise sizes its process pool from the host CPU count.
    constants.MAX_FORK_WORKERS = 1
    config = _config(root, trusted)
    # Reject symlinks even inside the root: loader discovery must not follow
    # unexpected trees. Ignore caches and installed dependencies entirely.
    inputs = [root / n for n in ("models", "audits", "macros", "signals", "materializations", "linter", "seeds", "metrics", "tests", "external_models")]
    inputs += [root / n for n in ("external_models.yaml", "schema.yaml")]
    files: list[Path] = []
    for item in inputs:
        if item.is_symlink():
            raise ExportError("Symlink project inputs are unsupported")
        if item.is_dir():
            for parent, dirs, names in os.walk(item, followlinks=False):
                for name in dirs + names:
                    p = Path(parent) / name
                    if p.is_symlink():
                        raise ExportError("Symlink project inputs are unsupported")
                files.extend(Path(parent) / name for name in names)
        elif item.is_file():
            files.append(item)
    for path in files:
        contained(path, root)
        if path.suffix == ".py" and not trusted:
            raise ExportError("Python project files require --trust-project")
        if path.suffix in (".yaml", ".yml"):
            content = path.read_text(encoding="utf-8")
            if any(t in content for t in ("{{", "{%", "{#")):
                raise ExportError("Templated input YAML is unsupported")
            if path.name in ("external_models.yaml", "schema.yaml") or path.is_relative_to(root / "external_models"):
                rows = YAML(typ="safe").load(content)
                allowed = {"name", "dialect", "columns", "column_descriptions", "description", "gateway", "owner", "tags", "cron", "grain", "grains", "audits"}
                if rows is not None and (not isinstance(rows, list) or any(not isinstance(row, dict) or row.keys() - allowed for row in rows)):
                    raise ExportError("External model YAML accepts metadata fields only")
                if not trusted:
                    for row in rows or []:
                        for field in ("columns", "grain", "grains", "audits"):
                            if "@" in str(row.get(field, "")):
                                raise ExportError("Executable external model fields require --trust-project")
        if path.suffix == ".sql":
            _check_sql(path, root, config.dialect, trusted)
    with TemporaryDirectory(prefix="sqlmesh-dbt-manifest-") as cache:
        config.cache_dir = cache
        config.disable_anonymized_analytics = True
        config.notification_targets = []
        config.users = []
        context = OfflineContext(config={root: config}, gateway=gateway, load=False, load_state=False, concurrent_tasks=1)
        # SQLMesh's Noop dispatcher still creates a local telemetry user file.
        # Keep that file inside scratch too, without consulting the user's home.
        previous_collector = analytics.collector
        analytics.collector = AnalyticsCollector(dispatcher=NoopEventDispatcher(), sqlmesh_path=Path(cache))
        try:
            context.load()
            if not context.models:
                raise ExportError("No SQLMesh models found in the project")
            yield context
        finally:
            analytics.collector.shutdown(flush=False)
            analytics.collector = previous_collector
