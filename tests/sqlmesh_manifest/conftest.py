from pathlib import Path
import shutil

import pytest

# These tests need the optional `sqlmesh` extra (sqlmesh, dbt-artifacts-parser,
# jsonschema, ...). Every test module in this directory starts with
# `pytest.importorskip("sqlmesh")` / `pytest.importorskip("dbt_artifacts_parser")`,
# so a default test run without the extra reports them as skipped. This conftest
# therefore imports the subpackage lazily: it must stay importable without the
# extra, including when this directory is passed to pytest explicitly.

EXAMPLE = Path(__file__).resolve().parents[2] / 'examples' / 'sqlmesh_manifest' / 'mini_project'


@pytest.fixture
def project(tmp_path):
    return Path(shutil.copytree(EXAMPLE, tmp_path / 'project'))


@pytest.fixture(scope='session')
def example():
    from datawrench.sqlmesh_manifest.exporter import build_manifest
    from datawrench.sqlmesh_manifest.loading import load_project

    with load_project(EXAMPLE) as context:
        manifest, warnings = build_manifest(context, deterministic=True)
        yield context, manifest, warnings


@pytest.fixture(scope='session')
def diff_schema():
    import json
    from importlib.resources import files
    from jsonschema import Draft7Validator

    schema = json.loads(files('datawrench.sqlmesh_manifest').joinpath('diff-v1.json').read_text())
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)
