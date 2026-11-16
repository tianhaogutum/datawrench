"""Documentation counts follow validated manifest resource groups."""
import copy
import io
import json
import sys
from types import SimpleNamespace

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.validation import validate_manifest
from .test_manifest_validation_json import LEGACY


DOCS_TEXT = 'models=0 seeds=0 sources=0 tests=0 docs=1 edges=0\n'


@pytest.mark.parametrize('mode', ['file', 'stdin'])
def test_retained_v1_docs_regression(monkeypatch, capsys, mode):
    if mode == 'stdin':
        monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(LEGACY.read_bytes())))
    assert main(['summary', str(LEGACY) if mode == 'file' else '-']) == 0
    assert capsys.readouterr() == (DOCS_TEXT, '')


def summarize(data, mode, monkeypatch, capsys, tmp_path, expected):
    """Assert the real validators and counter preserve caller-owned input."""
    from datawrench.sqlmesh_manifest import cli

    before = copy.deepcopy(data)
    validate_manifest(data)
    assert data == before
    content = json.dumps(data).encode()
    path = tmp_path / 'manifest.json'
    path.write_bytes(content)
    stream = io.BytesIO(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    original = cli.read_manifest_input

    def observe(token):
        parsed = original(token)
        assert parsed == before
        return data

    monkeypatch.setattr(cli, 'read_manifest_input', observe)
    assert main(['summary', str(path) if mode == 'file' else '-']) == 0
    assert capsys.readouterr() == (expected, '')
    assert data == before and path.read_bytes() == content
    assert stream.getvalue() == content and not stream.closed
    assert stream.tell() == (0 if mode == 'file' else len(content))


@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('version', range(1, 12))
def test_nonempty_legacy_docs(monkeypatch, capsys, tmp_path, mode, version):
    data = json.loads(LEGACY.read_text())
    data['metadata']['dbt_schema_version'] = f'https://schemas.getdbt.com/dbt/manifest/v{version}.json'
    if version >= 4:
        data['metrics'] = {}
    if version >= 8:
        doc = data['docs']['doc.legacy.overview']
        del doc['root_path']
        doc['resource_type'] = 'doc'
    if version >= 9:
        data.update(groups={}, group_map={})
    if version >= 10:
        data['semantic_models'] = {}
    if version >= 11:
        data.update(disabled={}, parent_map={}, child_map={}, saved_queries={})
    summarize(data, mode, monkeypatch, capsys, tmp_path, DOCS_TEXT)


@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('parents', ['normal', 'null', 'both-null'])
def test_v12_docs_extra_labels_and_edges(monkeypatch, capsys, tmp_path, example, mode, parents):
    data = copy.deepcopy(example[1])
    doc = copy.deepcopy(json.loads(LEGACY.read_text())['docs']['doc.legacy.overview'])
    del doc['root_path']
    doc['resource_type'] = 'doc'  # Required by the official v12 schema.
    data['docs'] = {doc['unique_id']: doc}
    legacy = json.loads(LEGACY.with_name('v1-summary.json').read_text())
    macro = legacy['macros']['macro.legacy.example']
    del macro['root_path']
    data['macros'] = {macro['unique_id']: macro}
    model = copy.deepcopy(data['nodes']['model.mini_project.stg_orders'])
    model.update(resource_type='analysis', unique_id='analysis.mini_project.overview')
    model['depends_on']['nodes'] = []
    data['nodes'][model['unique_id']] = model
    data['parent_map'][model['unique_id']] = []
    data['child_map'][model['unique_id']] = []
    data['disabled'] = {model['unique_id']: [copy.deepcopy(model)]}
    data['selectors'] = {'synthetic': {'definition': {'method': 'fqn', 'value': '*'}}}
    if parents != 'normal':
        data['parent_map'] = None
    if parents == 'both-null':
        data['child_map'] = None
    expected = ('models=3 seeds=1 sources=1 tests=2 analyses=1 docs=1 macros=1 '
                f"edges={5 if parents == 'normal' else 0}\n")
    summarize(data, mode, monkeypatch, capsys, tmp_path, expected)


@pytest.mark.parametrize('mode', ['file', 'stdin'])
def test_legacy_mixed_labels(monkeypatch, capsys, tmp_path, mode):
    data = json.loads(LEGACY.with_name('v1-summary.json').read_text())
    data['docs'] = json.loads(LEGACY.read_text())['docs']
    summarize(data, mode, monkeypatch, capsys, tmp_path,
              'models=0 seeds=0 sources=0 tests=0 analyses=1 docs=1 macros=1 edges=0\n')


@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('problem', ['legacy-doc', 'v12-missing-type', 'v12-wrong-type'])
def test_docs_still_validated(monkeypatch, capsys, tmp_path, example, mode, problem):
    data = json.loads(LEGACY.read_text())
    if problem == 'legacy-doc':
        del data['docs']['doc.legacy.overview']['block_contents']
    else:
        doc = data['docs']['doc.legacy.overview']
        del doc['root_path']
        if problem == 'v12-wrong-type':
            doc['resource_type'] = 'model'
        data = copy.deepcopy(example[1])
        data['docs'] = {doc['unique_id']: doc}
    content = json.dumps(data).encode()
    path = tmp_path / 'manifest.json'
    path.write_bytes(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(content)))
    assert main(['summary', str(path) if mode == 'file' else '-']) == 1
    out, err = capsys.readouterr()
    assert out == '' and err.startswith('validation error: $.docs.')
    assert len(err) < 2048 and 'KeyError' not in err


@pytest.mark.parametrize('mode', ['file', 'stdin'])
def test_docs_offline_read_only(monkeypatch, capsys, tmp_path, mode):
    import requests
    import socket
    from datawrench.sqlmesh_manifest import cli

    def blocked(*args, **kwargs):
        pytest.fail('Summary must not load projects, use network or write artifacts')

    for name in ('load_project', 'build_manifest', '_write_atomic'):
        monkeypatch.setattr(cli, name, blocked)
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    summarize(json.loads(LEGACY.read_text()), mode, monkeypatch, capsys, tmp_path, DOCS_TEXT)
