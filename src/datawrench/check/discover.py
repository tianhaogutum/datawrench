"""Find the inputs of a data repository by their content, not their names.

Only these are opened: ``*.py``, ``*.sql``, ``*.csv``, ``*.parquet`` (footers
are read later, by the check), ``*.json``, ``*.jsonl``, ``*.ndjson``,
``*.singer``, ``dbt_project.yml``, ``.sqlfluff`` and the ``pyproject.toml`` /
``setup.cfg`` / ``tox.ini`` files that may hold a SQLFluff section. A file that
is not a known input (a ``package.json``, a Python file that does not import
Polars) is skipped silently. Nothing is written, nothing is executed and no
symlinked directory is followed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any

from .model import INFO, WARNING, Finding

# Directories that never hold inputs worth checking. Dot-directories are skipped too.
IGNORE_DIRS = frozenset({"node_modules", "__pycache__", "venv", "site-packages", "dbt_packages", "dbt_modules"})
# dbt build output: only the files directly in target/ (manifest.json) are read,
# never target/compiled or target/run (pass target/compiled explicitly to audit it).
BUILD_DIRS = frozenset({"target"})
MAX_CANDIDATES = 50_000
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
SNIFF_BYTES = 1024 * 1024

POLARS_IMPORT = re.compile(r"^[ \t]*(?:import[ \t]+polars\b|from[ \t]+polars\b)", re.MULTILINE)
MIGRATE_UP = re.compile(r"^[ \t]*--[ \t]*migrate:up\b", re.MULTILINE)
DUCKMATE_SNAPSHOT = "-- duckmate schema-only snapshot"
JINJA = re.compile(r"\{\{|\{%|\{#")
LEADING_COMMENTS = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)
SQLMESH_HEADER = re.compile(r"(?:MODEL|AUDIT|METRIC)\s*\(", re.IGNORECASE)
HIVE_PART = re.compile(r"[^=/]+=[^/]*")
SQLFLUFF_SECTION = re.compile(r"^\s*\[(?:tool\.)?sqlfluff(?:[:.\]])", re.MULTILINE)
SQLFLUFF_HOSTS = frozenset({"pyproject.toml", "setup.cfg", "tox.ini"})
SQLMESH_CONFIGS = ("config.yaml", "config.yml", "config.py")
SQLMESH_MARKERS = re.compile(r"\bmodel_defaults\b|\bgateways\b|\bsqlmesh\b")
CASSETTE_FILES = frozenset({"manifest.json", "messages.jsonl.gz", "catalog.json"})
MESSAGE_SUFFIXES = (".jsonl", ".ndjson", ".singer")
# Names that announce a JSON input: such files are reported when they do not parse.
ANNOUNCED_JSON = frozenset({"manifest.json", "catalog.json"})

KINDS = {
    "dbt-project": "dbt project",
    "dbt-manifest": "dbt manifest",
    "sqlmesh-project": "SQLMesh project",
    "sql": "SQL file",
    "sqlfluff-config": "SQLFluff config",
    "python-polars": "Polars script",
    "csv": "CSV file",
    "parquet-dataset": "Parquet dataset",
    "duckmate-migrations": "migrations folder",
    "singer-messages": "Singer/Airbyte message file",
    "singer-catalog": "Singer/Airbyte catalog",
    "singer-cassette": "singer-replay cassette",
}
FOLDER_KINDS = frozenset({"dbt-project", "sqlmesh-project", "parquet-dataset", "duckmate-migrations",
                          "singer-cassette"})


class DiscoveryError(ValueError):
    """A path given on the command line cannot be used (exit 2)."""


@dataclass
class Source:
    """One detected input.

    ``path`` is a file, or a folder for the FOLDER_KINDS. ``files`` lists the
    member files of a Parquet dataset as (path, display) pairs. ``data`` is
    the parsed JSON of a Singer/Airbyte catalog. ``meta`` holds what discovery
    learnt while sniffing (a manifest's adapter type, whether SQL has Jinja).
    """

    path: Path
    display: str
    kind: str
    data: Any = None
    files: tuple = ()
    meta: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return KINDS[self.kind]


@dataclass
class Discovery:
    sources: list[Source] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    scanned: int = 0
    # Collected while walking, turned into sources at the end.
    _parquet: list = field(default_factory=list, repr=False)
    _migrations: dict = field(default_factory=dict, repr=False)
    _dbt_roots: list = field(default_factory=list, repr=False)

    def of(self, *kinds: str) -> list[Source]:
        return [source for source in self.sources if source.kind in kinds]


# --------------------------------------------------------------------------- classification

def manifest_meta(data: Any) -> dict | None:
    """Facts about a dbt manifest (``target/manifest.json``), or None if it is not one."""
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata")
    version = metadata.get("dbt_schema_version") if isinstance(metadata, dict) else None
    if not isinstance(version, str) or "/manifest/" not in version or not isinstance(data.get("nodes"), dict):
        return None
    enforced = 0
    models = 0
    for node in data["nodes"].values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        models += 1
        contract = node.get("contract")
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if not isinstance(contract, dict):
            contract = config.get("contract") if isinstance(config.get("contract"), dict) else {}
        if contract.get("enforced") is True:
            enforced += 1
    adapter = metadata.get("adapter_type")
    return {"schema_version": version, "adapter_type": adapter if isinstance(adapter, str) else None,
            "project_name": metadata.get("project_name") if isinstance(metadata.get("project_name"), str) else None,
            "models": models, "enforced": enforced}


def is_catalog(data: Any) -> bool:
    """A Singer discovery catalog, or an Airbyte discovery / configured catalog."""
    if not isinstance(data, dict):
        return False
    streams = data.get("streams")
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        return False
    first = streams[0]
    if "tap_stream_id" in first and isinstance(first.get("schema"), dict):
        return True
    inner = first.get("stream", first)
    return isinstance(inner, dict) and isinstance(inner.get("json_schema"), dict) and isinstance(inner.get("name"), str)


def message_protocol(line: bytes) -> str | None:
    """``singer`` or ``airbyte`` if one JSONL line is a protocol message, else None."""
    try:
        message = json.loads(line.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(message, dict):
        return None
    try:
        from datawrench.tapcheck.io import identify
        return identify(message)
    except ImportError:  # the tapcheck subpackage is unavailable: a conservative fallback
        kind = message.get("type")
        if isinstance(kind, str) and kind.upper() in {"SCHEMA", "RECORD", "STATE"} and "stream" in message:
            return "singer"
        return None
    except Exception:  # noqa: BLE001 - an ambiguous message is "not recognised" here
        return None


def _first_line(path: Path) -> bytes:
    with path.open("rb") as stream:
        while True:
            line = stream.readline(SNIFF_BYTES)
            if not line:
                return b""
            if line.strip():
                return line


def _read_text(path: Path, limit: int = MAX_TEXT_BYTES) -> str | None:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        return None
    return raw.decode("utf-8-sig", errors="replace")


def _read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(f"larger than {MAX_JSON_BYTES // (1024 * 1024)} MiB; not inspected")
    return json.loads(raw.decode("utf-8-sig"))


def _parse_failure(display: str, rule: str, reason: str) -> Finding:
    return Finding("parse", rule, WARNING, f"cannot read this file: {reason}", display,
                   hint="the file may be truncated or hand-edited; it was not checked")


def _unrecognised(display: str, what: str) -> Finding:
    return Finding("parse", "unrecognised", INFO, f"{what}; skipped", display)


def _sqlmesh_project(here: Path, files: set[str], dirs: list[str]) -> bool:
    if "models" not in dirs or "dbt_project.yml" in files:
        return False
    for name in SQLMESH_CONFIGS:
        if name in files:
            try:
                text = _read_text(here / name, SNIFF_BYTES)
            except OSError:
                return False
            return bool(text and SQLMESH_MARKERS.search(text))
    return False


def _inspect(path: Path, display: str, explicit: bool, result: Discovery) -> None:
    name = path.name
    lower = name.lower()
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            result.scanned += 1
            text = _read_text(path)
            if text is not None and POLARS_IMPORT.search(text):
                result.sources.append(Source(path, display, "python-polars"))
            elif explicit:
                result.findings.append(_unrecognised(display, "does not import polars (or is over 2 MiB)"))
        elif suffix == ".sql":
            result.scanned += 1
            text = _read_text(path)
            if text is None:
                if explicit:
                    result.findings.append(_unrecognised(display, "over 2 MiB"))
                return
            if text.startswith(DUCKMATE_SNAPSHOT):
                return  # written by `duckmate dump`, not hand-written SQL
            if MIGRATE_UP.search(text):
                parent = os.path.dirname(display) or "."
                result._migrations.setdefault(str(path.parent.resolve()), (path.parent, parent))
                return
            head = text[LEADING_COMMENTS.match(text).end():]
            if SQLMESH_HEADER.match(head):
                if explicit:
                    result.findings.append(_unrecognised(display, "a SQLMesh model or audit definition"))
                return
            result.sources.append(Source(path, display, "sql", meta={"jinja": bool(JINJA.search(text))}))
        elif suffix == ".csv":
            result.scanned += 1
            result.sources.append(Source(path, display, "csv"))
        elif suffix == ".parquet":
            result.scanned += 1
            result._parquet.append((path, display))
        elif lower == "dbt_project.yml":
            result.scanned += 1
            folder = os.path.dirname(display) or "."
            result.sources.append(Source(path.parent, folder, "dbt-project"))
            result._dbt_roots.append(path.parent.resolve())
        elif lower == ".sqlfluff" or lower in SQLFLUFF_HOSTS:
            result.scanned += 1
            text = _read_text(path, SNIFF_BYTES)
            if lower == ".sqlfluff" or (text and SQLFLUFF_SECTION.search(text)):
                result.sources.append(Source(path, display, "sqlfluff-config"))
        elif suffix in MESSAGE_SUFFIXES:
            result.scanned += 1
            protocol = message_protocol(_first_line(path))
            if protocol:
                result.sources.append(Source(path, display, "singer-messages", meta={"protocol": protocol}))
            elif explicit:
                result.findings.append(_unrecognised(display, "the first line is not a Singer or Airbyte message"))
        elif suffix == ".json":
            result.scanned += 1
            _inspect_json(path, display, explicit, result)
        elif explicit:
            result.findings.append(_unrecognised(display, "not a recognised input type"))
    except OSError as exc:
        result.findings.append(_parse_failure(display, "unreadable", exc.strerror or type(exc).__name__))


def _inspect_json(path: Path, display: str, explicit: bool, result: Discovery) -> None:
    announced = explicit or path.name.lower() in ANNOUNCED_JSON
    try:
        data = _read_json(path)
    except UnicodeError:
        if announced:
            result.findings.append(_parse_failure(display, "invalid-encoding", "not UTF-8 text"))
        return
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, json.JSONDecodeError) and exc.msg == "Extra data":
            # Tap output redirected to a .json file is JSON Lines, not one document.
            protocol = message_protocol(_first_line(path))
            if protocol:
                result.sources.append(Source(path, display, "singer-messages", meta={"protocol": protocol}))
                return
        if announced:
            reason = (f"invalid JSON ({exc.msg} at line {exc.lineno})" if isinstance(exc, json.JSONDecodeError)
                      else str(exc) if isinstance(exc, ValueError) else "nesting too deep")
            result.findings.append(_parse_failure(display, "invalid-json", reason))
        return
    meta = manifest_meta(data)
    if meta is not None:
        result.sources.append(Source(path, display, "dbt-manifest", meta=meta))
    elif is_catalog(data):
        result.sources.append(Source(path, display, "singer-catalog", data=data))
    elif explicit:
        result.findings.append(_unrecognised(display, "not a dbt manifest or a Singer/Airbyte catalog"))


# --------------------------------------------------------------------------- walking

def _walk(root: Path, raw: str, result: Discovery, seen: set) -> None:
    def fail(error: OSError):
        raise DiscoveryError(f"cannot read directory {error.filename}: {error.strerror}")

    for parent, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        here = Path(parent)
        folder = os.path.normpath(os.path.join(raw, here.relative_to(root)))
        names = set(files)
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")
                         and not (here / d).is_symlink())
        if CASSETTE_FILES <= names:
            result.scanned += 1
            result.sources.append(Source(here, folder, "singer-cassette"))
            dirs[:] = []
            continue
        if _sqlmesh_project(here, names, dirs):
            result.sources.append(Source(here, folder, "sqlmesh-project"))
        if here.name in BUILD_DIRS and here != root:
            dirs[:] = []
        for name in sorted(files):
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > MAX_CANDIDATES:
                raise DiscoveryError(f"more than {MAX_CANDIDATES} files; check a smaller folder")
            _inspect(path, os.path.normpath(os.path.join(folder, name)), False, result)


def _dataset_root(path: Path, display: str) -> tuple[Path, str]:
    """The folder a Parquet file belongs to, above any Hive ``key=value`` partition folders."""
    folder, shown = path.parent, os.path.dirname(display)
    while HIVE_PART.fullmatch(folder.name) and shown not in ("", ".") and HIVE_PART.fullmatch(os.path.basename(shown)):
        folder, shown = folder.parent, os.path.dirname(shown)
    return folder, shown or "."


def _finish(result: Discovery) -> None:
    datasets: dict[str, tuple[Path, str, list]] = {}
    for path, display in result._parquet:
        folder, shown = _dataset_root(path, display)
        datasets.setdefault(str(folder.resolve()), (folder, shown, []))[2].append((path, display))
    for folder, shown, members in datasets.values():
        result.sources.append(Source(folder, shown, "parquet-dataset", files=tuple(sorted(members, key=lambda m: m[1]))))
    for folder, shown in result._migrations.values():
        result.sources.append(Source(folder, shown, "duckmate-migrations"))
    if result._dbt_roots:
        # CSV files inside a dbt project are seeds: the project check owns them.
        def in_project(source: Source) -> bool:
            resolved = source.path.resolve()
            return any(resolved.is_relative_to(root) for root in result._dbt_roots)
        result.sources = [s for s in result.sources if not (s.kind == "csv" and in_project(s))]
    result._parquet.clear()
    result._migrations.clear()
    result.sources.sort(key=lambda source: (source.display, source.kind))


def discover(paths) -> Discovery:
    """Walk files and directories; raise DiscoveryError for unusable arguments."""
    result = Discovery()
    seen: set = set()
    for raw in paths:
        raw = str(raw)
        if not raw:
            raise DiscoveryError("empty path; use . for the current directory")
        path = Path(raw)
        if not path.exists():
            raise DiscoveryError(f"no such file or directory: {raw}")
        if path.is_dir():
            _walk(path, raw, result, seen)
            continue
        if not path.is_file():
            raise DiscoveryError(f"not a regular file or directory: {raw}")
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        _inspect(path, os.path.normpath(raw), True, result)
    _finish(result)
    return result
