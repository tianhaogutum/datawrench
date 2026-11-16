"""Machine-readable report validation: safe, deterministic, bounded results."""
import json
import copy
import hashlib
from importlib.resources import files

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from jsonschema import Draft7Validator

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.exporter import serialize

from .test_report_validation import entry, pair, rewrite_report  # shared real fixture


def result(pair, capsys, expected):
    before = [p.read_bytes() if p.exists() else None for p in pair]
    args = ['validate-report', str(pair[1]), '--manifest', str(pair[0]), '--format', 'json']
    assert main(args) == expected
    capture = capsys.readouterr()
    assert capture.err == ''
    data = json.loads(capture.out)
    schema = json.loads(files('datawrench.sqlmesh_manifest').joinpath('report-validation-result-v1.json').read_text())
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(data)
    assert capture.out == serialize(data)
    assert len(capture.out.encode()) < 1024
    assert 'SECRET' not in capture.out
    assert main(args) == expected
    assert capsys.readouterr() == capture
    assert [p.read_bytes() if p.exists() else None for p in pair] == before
    return data


def test_json_valid_result(pair, capsys):
    assert result(pair, capsys, 0) == {
        'validation_version': 1, 'valid': True, 'report_version': 1,
        'counts': {'resources': 7, 'models': 3, 'seeds': 1, 'sources': 1,
                   'tests': 2, 'dependencies': 5},
        'diagnostics': [],
    }


@pytest.mark.parametrize('change,path,rule', [
    (lambda d: d.update(manifest_sha256='0' * 64), '$.manifest_sha256', 'manifest_sha256'),
    (lambda d: d.update(project_name='SECRET'), '$.project_name', 'project_name'),
    (lambda d: d['resources'].pop(), '$.resources', 'resources'),
    (lambda d: d['resources'].append(copy.deepcopy(d['resources'][0])), '$.resources', 'duplicate_resources'),
    (lambda d: entry(d)['origin'].update(file='SECRET'), None, 'origin'),
    (lambda d: entry(d)['dependencies'].clear(), None, 'dependencies'),
    (lambda d: entry(d)['dependencies'][0].update(sqlmesh_name='SECRET'), None, 'dependency_provenance'),
    (lambda d: entry(d).pop('origin'), None, 'schema.required'),
    (lambda d: entry(d).update(SECRET='SECRET'), None, 'schema.additionalProperties'),
    (lambda d: entry(d)['origin'].update(file={'SECRET': 'SECRET'}), None, 'schema.type'),
])
def test_json_tampered_report(pair, capsys, change, path, rule):
    rewrite_report(pair, change)
    data = result(pair, capsys, 1)
    assert data['valid'] is False
    assert data['report_version'] is data['counts'] is None
    diagnostic, = data['diagnostics']
    assert diagnostic['rule'] == rule
    if path is not None:
        assert diagnostic['path'] == path
    else:
        index = next(i for i, resource in enumerate(json.loads(pair[1].read_text())['resources'])
                     if resource['dbt_node_id'] == 'model.mini_project.fct_orders')
        assert diagnostic['path'].startswith(f'$.resources[{index}]')


@pytest.mark.parametrize('version', [None, True, 1.0, 'SECRET', 0, 2])
def test_json_unsupported_report_version(pair, capsys, version):
    rewrite_report(pair, lambda d: d.update(report_version=version))
    assert result(pair, capsys, 1)['diagnostics'] == [
        {'input': 'report', 'path': '$.report_version', 'rule': 'report_version'}]


@pytest.mark.parametrize('side', [0, 1])
@pytest.mark.parametrize('content', ['{"SECRET":', 'null', '{"SECRET":1,"SECRET":2}', '{"SECRET":NaN}', b'\xff'])
def test_json_invalid_input(pair, capsys, side, content):
    pair[side].write_bytes(content.encode() if isinstance(content, str) else content)
    assert result(pair, capsys, 1)['diagnostics'] == [
        {'input': 'manifest' if side == 0 else 'report', 'path': '$', 'rule': 'read_json'}]


@pytest.mark.parametrize('side', [0, 1])
@pytest.mark.parametrize('problem', ['missing', 'oversized'])
def test_json_unreadable_and_oversized_inputs(pair, capsys, monkeypatch, side, problem):
    import datawrench.sqlmesh_manifest.validation as validation

    if problem == 'missing':
        pair[side].unlink()
    else:
        limit = max(p.stat().st_size for p in pair) + 1
        monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', limit)
        pair[side].write_bytes(pair[side].read_bytes() + b' ' * limit)
    assert result(pair, capsys, 1)['diagnostics'][0] == {
        'input': 'manifest' if side == 0 else 'report', 'path': '$', 'rule': 'read_json'}


@pytest.mark.parametrize('problem', ['mismatched', 'version', 'schema', 'graph'])
def test_json_manifest_failure(pair, capsys, problem):
    manifest = json.loads(pair[0].read_text())
    if problem == 'mismatched':
        manifest['metadata']['invocation_id'] = '00000000-0000-0000-0000-000000000001'
    elif problem == 'version':
        manifest['metadata']['dbt_schema_version'] = 'SECRET'
    elif problem == 'schema':
        # Deliberately put a secret in a dictionary key in a schema error path.
        manifest['nodes']['SECRET'] = {'SECRET': 'SECRET'}
    else:
        node = next(n for n in manifest['nodes'].values() if n['resource_type'] == 'model')
        node['depends_on']['nodes'] = ['model.SECRET']
        manifest['parent_map'] = manifest['child_map'] = {}
    pair[0].write_text(serialize(manifest))
    if problem != 'mismatched':
        rewrite_report(pair, lambda d: d.update(manifest_sha256=hashlib.sha256(pair[0].read_bytes()).hexdigest()))
    diagnostic, = result(pair, capsys, 1)['diagnostics']
    assert diagnostic == {
        'input': 'pair' if problem == 'mismatched' else 'manifest',
        'path': '$.manifest_sha256' if problem == 'mismatched' else '$.metadata.dbt_schema_version' if problem == 'version' else '$',
        'rule': 'manifest_sha256' if problem == 'mismatched' else 'manifest_version' if problem == 'version' else 'manifest_validation',
    }


def test_json_is_offline_and_hides_internal_exception_details(pair, capsys, monkeypatch):
    import socket
    import requests
    import datawrench.sqlmesh_manifest.cli as cli

    def blocked(*args, **kwargs):
        pytest.fail('Project/network access is forbidden')

    monkeypatch.setattr(cli, 'load_project', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    assert result(pair, capsys, 0)['valid'] is True

    class SECRET_Exception(Exception):
        pass

    def crash(*args, **kwargs):
        raise SECRET_Exception('SECRET credentials /path/SECRET')

    monkeypatch.setattr(cli, 'validate_report', crash)
    assert result(pair, capsys, 1)['diagnostics'] == [
        {'input': 'pair', 'path': '$', 'rule': 'internal_error'}]


@pytest.mark.parametrize('problem', ['missing_manifest', 'format'])
def test_json_argument_errors_retain_exit_two(pair, capsys, problem):
    args = [] if problem == 'missing_manifest' else ['--manifest', str(pair[0]), '--format', 'yaml']
    with pytest.raises(SystemExit) as exc:
        main(['validate-report', str(pair[1]), '--format', 'json', *args])
    assert exc.value.code == 2
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'error:' in capture.err


@pytest.mark.parametrize('mode', [[], ['--format', 'text']])
def test_default_and_explicit_text(pair, capsys, mode):
    args = ['validate-report', str(pair[1]), '--manifest', str(pair[0]), *mode]
    assert main(args) == 0
    assert capsys.readouterr() == ('Valid provenance report v1 for supplied manifest\n', '')
    rewrite_report(pair, lambda d: d.update(manifest_sha256='0' * 64))
    assert main(args) == 1
    assert capsys.readouterr() == ('', 'validation error: $.manifest_sha256: does not match supplied manifest bytes\n')


def test_json_counts_use_validated_seed_edges_and_ignore_order(pair, capsys):
    manifest = json.loads(pair[0].read_text())
    source = next(iter(manifest['sources']))
    seed = 'seed.mini_project.raw_orders'
    manifest['parent_map'] = None
    manifest['child_map'][source].append(seed)
    pair[0].write_text(serialize(manifest))

    def change(report):
        entry(report, seed)['dependencies'] = [{
            'dbt_node_id': source, 'file': manifest['sources'][source]['original_file_path'],
            'sqlmesh_name': manifest['sources'][source]['meta']['sqlmesh_name']}]
        report['manifest_sha256'] = hashlib.sha256(pair[0].read_bytes()).hexdigest()
        report['resources'].reverse()
        for resource in report['resources']:
            resource['dependencies'].reverse()

    rewrite_report(pair, change)
    assert result(pair, capsys, 0)['counts']['dependencies'] == 6
