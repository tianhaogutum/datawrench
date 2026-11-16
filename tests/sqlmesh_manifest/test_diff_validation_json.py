"""Bounded JSON validation of saved diffs, independent of report payloads."""
import json

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main


def test_json_format_regression(tmp_path, capsys):
    path = tmp_path / 'diff.json'
    kinds = ('model_added', 'model_removed', 'dependencies_changed', 'column_added',
             'column_removed', 'column_changed', 'source_added', 'source_removed')
    path.write_text(json.dumps({'diff_version': 1, 'has_changes': False,
                                'summary': dict.fromkeys(kinds, 0), 'changes': []}))
    assert main(['validate-diff', str(path), '--format', 'json']) == 0
    out, err = capsys.readouterr()
    assert err == ''
    assert json.loads(out) == {'diff_validation_version': 1, 'valid': True, 'diagnostics': []}


# Reuse the saved-report fixtures from the text contract, including all eight
# original kinds. These are report inputs, not reconstructed manifests.
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from datawrench.sqlmesh_manifest.exporter import serialize
from .test_diff_validation import KINDS, FAILURE, SUCCESS, changed, unchanged


@pytest.fixture(scope='module')
def result_schema():
    schema = json.loads(files('datawrench.sqlmesh_manifest').joinpath('diff-validation-result-v1.json').read_text())
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def expected(valid):
    return {'diff_validation_version': 1, 'valid': valid,
            'diagnostics': [] if valid else [{'path': '$', 'rule': 'invalid_diff'}]}


def capture(path, capsys, result_schema, valid):
    assert main(['validate-diff', str(path), '--format', 'json']) == (0 if valid else 1)
    out, err = capsys.readouterr()
    assert err == ''
    data = json.loads(out)
    result_schema.validate(data)
    assert data == expected(valid)
    assert out == serialize(data)
    assert len(out.encode()) < 256
    assert 'SECRET' not in out
    return out


def check(tmp_path, capsys, result_schema, data, valid):
    path = tmp_path / 'SECRET-diff.json'
    content = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
    path.write_bytes(content)
    out = capture(path, capsys, result_schema, valid)
    assert path.read_bytes() == content
    return out


@pytest.mark.parametrize('kind', [None, *KINDS, 'description_changed', 'future_kind'])
def test_valid_original_extended_and_future_reports(tmp_path, capsys, result_schema, kind):
    data = unchanged() if kind is None else changed(kind)
    if kind == 'description_changed':
        data['changes'][0].update(before='SECRET\n旧', after='SECRET\r\n新 🐍')
    check(tmp_path, capsys, result_schema, data, True)


def test_additive_structural_only_and_deterministic(tmp_path, capsys, result_schema):
    data = changed('description_changed')
    data['changes'][0].update(before='SECRET', after='SECRET')  # Equality allowed.
    data['changes'] *= 2  # Duplicates allowed.
    data['summary']['description_changed'] = 99  # No arithmetic.
    data['changes'].append(changed('future_kind')['changes'][0])
    data['summary']['future_kind'] = 0
    data.update(diff_version=1.0, SECRET={'SECRET' * 1000: ['SECRET'] * 10000})
    out = check(tmp_path, capsys, result_schema, data, True)
    data['summary'].pop('description_changed')  # Optional even with events.
    other = tmp_path / 'other-SECRET-path.json'
    other.write_text(json.dumps(data))
    assert capture(other, capsys, result_schema, True) == out
    assert capture(other, capsys, result_schema, True) == out


@pytest.mark.parametrize('problem', ['version', 'boolean_version', 'missing_counter',
    'negative_future_counter', 'missing_provenance', 'description_missing',
    'description_nonstring', 'description_reserved_payload', 'column_payload',
    'dependency_payload', 'false_with_changes'])
def test_invalid_report_shapes(tmp_path, capsys, result_schema, problem):
    data = changed('description_changed')
    event = data['changes'][0]
    event.update(before='SECRET', after='SECRET')
    if problem == 'version':
        data['diff_version'] = 2
    elif problem == 'boolean_version':
        data['diff_version'] = True
    elif problem == 'missing_counter':
        del data['summary']['source_added']
    elif problem == 'negative_future_counter':
        data['summary']['SECRET' * 1000] = -1
    elif problem == 'missing_provenance':
        del event['before_file']
    elif problem == 'description_missing':
        del event['before']
    elif problem == 'description_nonstring':
        event['after'] = {'SECRET': 'SECRET'}
    elif problem == 'description_reserved_payload':
        event['column'] = 'SECRET'
    elif problem == 'column_payload':
        data = changed('column_changed')
        del data['changes'][0]['after']['config']
    elif problem == 'dependency_payload':
        data = changed('dependencies_changed')
        data['changes'][0]['added'] = []
    else:
        data['has_changes'] = False
    check(tmp_path, capsys, result_schema, data, False)


@pytest.mark.parametrize('content', [b'{"SECRET":', b'null', b'[]', b'"SECRET"', b'\xff',
    b'{"SECRET":1,"SECRET":2}', b'{"nested":{"SECRET":1,"SECRET":2}}',
    b'{"SECRET":NaN}', b'{"SECRET":Infinity}', b'{"SECRET":-Infinity}',
    b'{"SECRET":1e999}', b'{"SECRET":-1e999}', b'[' * 2000])
def test_invalid_json_inputs(tmp_path, capsys, result_schema, content):
    check(tmp_path, capsys, result_schema, content, False)


@pytest.mark.parametrize('problem', ['missing', 'directory', 'oversized', 'permission'])
def test_unreadable_or_oversized(tmp_path, capsys, result_schema, monkeypatch, problem):
    from datawrench.sqlmesh_manifest import validation

    path = tmp_path / 'SECRET.json'
    if problem == 'directory':
        path.mkdir()
    elif problem in ('oversized', 'permission'):
        path.write_text(json.dumps(unchanged()))
    original = path.read_bytes() if path.is_file() else None
    with monkeypatch.context() as patch:
        if problem == 'oversized':
            patch.setattr(validation, 'MAX_MANIFEST_BYTES', 8)
        elif problem == 'permission':
            def denied(*args, **kwargs):
                raise PermissionError('SECRET')
            patch.setattr(Path, 'open', denied)
        capture(path, capsys, result_schema, False)
    assert (path.read_bytes() if path.is_file() else None) == original


@pytest.mark.parametrize('mode', [[], ['--format', 'text']])
def test_text_default_and_explicit_compatibility(tmp_path, capsys, mode):
    path = tmp_path / 'diff.json'
    path.write_text(json.dumps(unchanged()))
    assert main(['validate-diff', str(path), *mode]) == 0
    assert capsys.readouterr() == (SUCCESS, '')
    path.write_text('{')
    assert main(['validate-diff', str(path), *mode]) == 1
    assert capsys.readouterr() == ('', FAILURE)


@pytest.mark.parametrize('args', [[], ['--format', 'json'], ['x', '--format'],
    ['x', '--format', 'yaml'], ['x', 'extra', '--format', 'json'],
    ['x', '--out', 'y', '--format', 'json'], ['x', '--unknown', '--format', 'json']])
def test_json_argument_errors(args, capsys):
    with pytest.raises(SystemExit) as exc:
        main(['validate-diff', *args])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == ''
    assert 'usage:' in err and 'error:' in err


def test_offline_no_project_or_manifest_validation(tmp_path, capsys, result_schema, monkeypatch):
    import socket
    import requests
    from datawrench.sqlmesh_manifest import cli, validation

    def blocked(*args, **kwargs):
        pytest.fail('validate-diff must only read the artifact and packaged schema')

    for name in ('load_project', 'build_manifest', 'validate_manifest', 'read_manifest'):
        monkeypatch.setattr(cli, name, blocked)
    monkeypatch.setattr(validation, 'parse_manifest', blocked)
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    data = changed('future_kind')
    data.update({'$schema': 'https://SECRET.invalid/schema.json', '$ref': 'https://SECRET.invalid/data'})
    check(tmp_path, capsys, result_schema, data, True)


@pytest.mark.parametrize('exception', [RuntimeError('SECRET' * 10000),
    ValueError('SECRET /private/path')])
def test_unexpected_failure_is_fixed(tmp_path, capsys, result_schema, monkeypatch, exception):
    from datawrench.sqlmesh_manifest import diff_validation

    def broken(*args, **kwargs):
        raise exception

    monkeypatch.setattr(diff_validation, 'validate_diff', broken)
    check(tmp_path, capsys, result_schema, unchanged(), False)


@pytest.mark.parametrize('valid,change', [
    (True, lambda d: d.update(diff_validation_version=2)),
    (True, lambda d: d.update(diff_validation_version=True)),
    (True, lambda d: d.update(valid=1)),
    (True, lambda d: d.update(SECRET='SECRET')),
    (True, lambda d: d.update(diff_version=1)),
    (True, lambda d: d.pop('diagnostics')),
    (True, lambda d: d.update(diagnostics=[{'path': '$', 'rule': 'invalid_diff'}])),
    (False, lambda d: d.update(valid=True)),
    (False, lambda d: d.update(diagnostics=[])),
    (False, lambda d: d['diagnostics'].append(deepcopy(d['diagnostics'][0]))),
    (False, lambda d: d['diagnostics'][0].update(path='$.SECRET')),
    (False, lambda d: d['diagnostics'][0].update(rule='SECRET')),
    (False, lambda d: d['diagnostics'][0].update(message='SECRET')),
    (False, lambda d: d['diagnostics'][0].pop('rule')),
])
def test_result_schema_rejects_contract_drift(result_schema, valid, change):
    data = expected(valid)
    change(data)
    assert not result_schema.is_valid(data)
