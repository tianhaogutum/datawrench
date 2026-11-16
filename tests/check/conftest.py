"""Shared helpers for the ``datawrench check`` tests. Every fixture is synthetic."""
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from datawrench.check import cli  # noqa: E402

HERE = Path(__file__).resolve().parent
DEMO = HERE.parents[1] / "examples" / "check"
# Optional dependencies behind the checks that find something in the demo project.
DEMO_EXTRAS = ("sqlglot", "duckdb", "fsspec", "polars", "yaml", "jsonschema", "rfc3339_validator")


def needs(*modules):
    """Skip the calling test unless every optional dependency is importable."""
    for module in modules:
        pytest.importorskip(module)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A private copy of examples/check, with the working directory inside it."""
    target = tmp_path / "shop"
    shutil.copytree(DEMO, target)
    monkeypatch.chdir(target)
    return target


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """An empty working directory; returns it. Files are written with ``write``."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def check(capsys):
    """Run ``datawrench check`` in-process; return (exit code, stdout, stderr)."""
    def run(*argv):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:  # argparse usage errors and --help
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


@pytest.fixture
def check_json(check):
    """Run with --format json and return (exit code, parsed document)."""
    def run(*argv):
        code, out, err = check(*argv, "--format", "json")
        assert err == "", err
        return code, json.loads(out)
    return run


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_json(path: Path, value) -> Path:
    return write(path, json.dumps(value, indent=2))


def jsonl(path: Path, messages) -> Path:
    return write(path, "".join(json.dumps(message) + "\n" for message in messages))


def rule_ids(document, file=None):
    return sorted(f["ruleId"] for f in document["findings"] if file is None or f["file"] == file)


def findings(document, rule_id):
    return [f for f in document["findings"] if f["ruleId"] == rule_id]


def snapshot(root: Path) -> dict:
    """Every path under root with its bytes (files) or None (folders): to prove nothing was written."""
    result = {}
    for parent, dirs, files in os.walk(root):
        for name in dirs:
            result[os.path.relpath(os.path.join(parent, name), root)] = None
        for name in files:
            path = Path(parent) / name
            result[os.path.relpath(path, root)] = path.read_bytes()
    return result


# --------------------------------------------------------------------------- builders

def manifest(columns: dict, *, enforced=True, adapter="duckdb", name="orders", project="shop"):
    """A hand-trimmed dbt manifest with one model (the fields contract-diff reads)."""
    key = f"model.{project}.{name}"
    return {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                     "adapter_type": adapter, "project_name": project},
        "nodes": {key: {
            "resource_type": "model", "unique_id": key, "name": name, "package_name": project,
            "config": {"enabled": True, "contract": {"enforced": enforced}},
            "contract": {"enforced": enforced},
            "columns": {column: {"name": column, "data_type": dtype, "constraints": []}
                        for column, dtype in columns.items()},
            "constraints": [],
        }},
        "disabled": {},
    }


def singer_schema(stream="users", key="id"):
    return {"type": "SCHEMA", "stream": stream, "key_properties": [key],
            "schema": {"type": "object", "properties": {key: {"type": "integer"}, "name": {"type": "string"}}}}


def singer_record(stream="users", **record):
    return {"type": "RECORD", "stream": stream, "record": record}


SINGER_STATE = {"type": "STATE", "value": {"bookmarks": {}}}
