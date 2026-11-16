"""Standalone validation contract: old inputs, safe diagnostics and text parity."""
import copy
import json
from importlib.resources import files
from pathlib import Path

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from jsonschema import Draft7Validator

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.exporter import serialize
from datawrench.sqlmesh_manifest.validation import validate_manifest

LEGACY = Path(__file__).parent / 'fixtures' / 'manifest_validation' / 'v1.json'
TEXT = 'Valid dbt manifest (dbt-artifacts-parser; v12 also checked against vendored official schema)\n'


@pytest.fixture
def artifact(example, tmp_path):
    p = tmp_path / 'SECRET-manifest.json'
    p.write_text(serialize(example[1]))
    return p


def result(path, capsys, expected):
    before = path.read_bytes() if path.is_file() else None
    args = ['validate', str(path), '--format', 'json']
    assert main(args) == expected
    capture = capsys.readouterr()
    assert capture.err == ''
    data = json.loads(capture.out)
    schema = json.loads(files('datawrench.sqlmesh_manifest').joinpath('manifest-validation-result-v1.json').read_text())
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(data)
    assert capture.out == serialize(data)
    assert len(capture.out.encode()) < 1024
    assert 'SECRET' not in capture.out
    assert main(args) == expected
    assert capsys.readouterr() == capture
    assert (path.read_bytes() if path.is_file() else None) == before
    return data


def test_json_valid_manifest(artifact, capsys):
    assert result(artifact, capsys, 0) == {
        'manifest_validation_version': 1, 'valid': True, 'manifest_version': 12,
        'counts': {'resources': 7, 'models': 3, 'seeds': 1, 'sources': 1, 'tests': 2, 'edges': 5},
        'diagnostics': [],
    }


def test_retained_v1_input(capsys):
    validate_manifest(json.loads(LEGACY.read_text()))
    assert main(['validate', str(LEGACY)]) == 0
    assert capsys.readouterr() == (TEXT, '')
    data = result(LEGACY, capsys, 0)
    assert data['manifest_version'] == 1
    assert data['counts'] == {'resources': 1, 'models': 0, 'seeds': 0, 'sources': 0, 'tests': 0, 'edges': 0}


@pytest.mark.parametrize('content', ['{"SECRET":', 'null', '[]', '{"SECRET":1,"SECRET":2}', '{"SECRET":NaN}', '{"SECRET":Infinity}', b'\xff'])
def test_invalid_json(artifact, capsys, content):
    artifact.write_bytes(content.encode() if isinstance(content, str) else content)
    assert result(artifact, capsys, 1)['diagnostics'] == [{'path': '$', 'rule': 'read_json'}]


@pytest.mark.parametrize('problem', ['missing', 'directory', 'oversized'])
def test_unreadable_or_oversized(artifact, capsys, monkeypatch, problem):
    if problem == 'oversized':
        import datawrench.sqlmesh_manifest.validation as validation
        monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', artifact.stat().st_size - 1)
    else:
        artifact.unlink()
        if problem == 'directory':
            artifact.mkdir()
    assert result(artifact, capsys, 1)['diagnostics'] == [{'path': '$', 'rule': 'read_json'}]


@pytest.mark.parametrize('metadata', [None, [], {}, {'dbt_schema_version': True}, {'dbt_schema_version': 12}, {'dbt_schema_version': 'SECRET'}, {'dbt_schema_version': 'https://schemas.getdbt.com/dbt/manifest/v13.json'}])
def test_unsupported_version(artifact, capsys, metadata):
    d = json.loads(artifact.read_text())
    d['metadata'] = metadata
    artifact.write_text(serialize(d))
    assert result(artifact, capsys, 1)['diagnostics'] == [
        {'path': '$.metadata.dbt_schema_version', 'rule': 'manifest_version'}]


@pytest.mark.parametrize('problem', ['schema', 'parser', 'id', 'dangling', 'duplicates', 'omitted', 'reciprocal'])
def test_validation_stages_hide_arbitrary_keys(artifact, capsys, problem):
    d = json.loads(artifact.read_text())
    rule = 'graph_validation'
    if problem == 'schema':
        d['nodes']['SECRET'] = {'SECRET': 'SECRET'}
        rule = 'schema_validation'
    elif problem == 'parser':
        d = json.loads(LEGACY.read_text())
        d['docs']['SECRET'] = {'SECRET': 'SECRET'}
        rule = 'parser_validation'
    else:
        node = d['nodes']['model.mini_project.stg_orders']
        if problem == 'id':
            node['unique_id'] = 'model.SECRET'
        elif problem == 'dangling':
            node['depends_on']['nodes'] = ['model.SECRET']
            d['parent_map'] = d['child_map'] = {}
        elif problem == 'duplicates':
            node['depends_on']['nodes'] *= 2
        elif problem == 'omitted':
            d['parent_map'] = d['child_map'] = {}
        else:
            # Source -> seed edge: neither declares depends_on, so reciprocity
            # rather than declared model edges detects the inconsistent maps.
            source = next(iter(d['sources']))
            d['parent_map']['seed.mini_project.raw_orders'] = [source]
    artifact.write_text(serialize(d))
    assert result(artifact, capsys, 1)['diagnostics'] == [{'path': '$', 'rule': rule}]


@pytest.mark.parametrize('mode', [[], ['--format', 'text']])
def test_text_compatibility(artifact, capsys, mode):
    assert main(['validate', str(artifact), *mode]) == 0
    assert capsys.readouterr() == (TEXT, '')
    artifact.write_text('{')
    assert main(['validate', str(artifact), *mode]) == 1
    assert capsys.readouterr() == ('', 'validation error: Invalid or truncated JSON manifest\n')


@pytest.mark.parametrize('args', [[], ['--format', 'json'], ['x', '--format'], ['x', '--format', 'yaml'], ['x', 'extra', '--format', 'json'], ['x', '--unknown', '--format', 'json']])
def test_argument_errors(args, capsys):
    with pytest.raises(SystemExit) as exc:
        main(['validate', *args])
    assert exc.value.code == 2
    capture = capsys.readouterr()
    assert capture.out == '' and 'error:' in capture.err


@pytest.mark.parametrize('parents', [None, {}])
def test_counts_preserve_optional_graph_behavior(artifact, capsys, parents):
    d = json.loads(artifact.read_text())
    if parents is None:
        d['parent_map'] = None
    else:
        d['parent_map'] = d['child_map'] = {}
        for node in d['nodes'].values():
            if 'nodes' in node.get('depends_on', {}):
                node['depends_on']['nodes'] = []
    artifact.write_text(serialize(d))
    data = result(artifact, capsys, 0)
    assert data['counts']['edges'] == 0 and data['counts']['resources'] == 7


def test_json_offline_and_safe_internal_failure(artifact, capsys, monkeypatch):
    import socket
    import requests
    import datawrench.sqlmesh_manifest.cli as cli
    import datawrench.sqlmesh_manifest.validation as validation

    def blocked(*args, **kwargs):
        pytest.fail('Project/network access is forbidden')

    monkeypatch.setattr(cli, 'load_project', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    assert result(artifact, capsys, 0)['valid'] is True

    class SECRET_Exception(Exception):
        pass

    def crash(*args, **kwargs):
        raise SECRET_Exception('SECRET /path/SECRET token')

    monkeypatch.setattr(validation, 'validate_manifest', crash)
    assert result(artifact, capsys, 1)['diagnostics'] == [{'path': '$', 'rule': 'internal_error'}]


@pytest.mark.parametrize('change', [
    lambda d: d.update(manifest_validation_version=2),
    lambda d: d.update(manifest_version=13),
    lambda d: d.update(manifest_version=True),
    lambda d: d['counts'].update(SECRET=1),
    lambda d: d['counts'].update(edges=-1),
    lambda d: d['counts'].pop('resources'),
    lambda d: d.update(valid=False),
    lambda d: d.update(diagnostics=[{'path': '$.SECRET', 'rule': 'schema_validation'}]),
    lambda d: d.update(diagnostics=[{'path': '$', 'rule': 'manifest_version'}]),
])
def test_result_schema_rejects_invalid_success(artifact, capsys, change):
    data = result(artifact, capsys, 0)
    change(data)
    schema = json.loads(files('datawrench.sqlmesh_manifest').joinpath('manifest-validation-result-v1.json').read_text())
    assert not Draft7Validator(schema).is_valid(data)


@pytest.mark.parametrize('change', [
    lambda d: d.update(manifest_version=12),
    lambda d: d.update(counts={}),
    lambda d: d['diagnostics'].clear(),
    lambda d: d['diagnostics'].append(copy.deepcopy(d['diagnostics'][0])),
    lambda d: d['diagnostics'][0].update(path='$.SECRET'),
    lambda d: d['diagnostics'][0].update(rule='SECRET'),
    lambda d: d['diagnostics'][0].update(message='SECRET'),
])
def test_result_schema_rejects_invalid_failure(artifact, capsys, change):
    artifact.write_text('{')
    data = result(artifact, capsys, 1)
    change(data)
    schema = json.loads(files('datawrench.sqlmesh_manifest').joinpath('manifest-validation-result-v1.json').read_text())
    assert not Draft7Validator(schema).is_valid(data)
