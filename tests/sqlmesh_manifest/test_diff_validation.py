"""Default-text offline validation of saved diffs, with bounded diagnostics."""
import json
from copy import deepcopy

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.validation import ManifestError, read_json_artifact

KINDS = ('model_added', 'model_removed', 'dependencies_changed', 'column_added',
         'column_removed', 'column_changed', 'source_added', 'source_removed')
SUCCESS = 'Valid semantic diff v1 (structural validation only)\n'
FAILURE = 'validation error: Invalid or unsupported semantic diff report\n'


def unchanged():
    return {'diff_version': 1, 'has_changes': False,
            'summary': dict.fromkeys(KINDS, 0), 'changes': []}


def test_validate_diff_command(tmp_path, capsys):
    path = tmp_path / 'diff.json'
    path.write_text(json.dumps(unchanged()))
    before = path.read_bytes()
    assert main(['validate-diff', str(path)]) == 0
    assert capsys.readouterr() == (SUCCESS, '')
    assert path.read_bytes() == before


@pytest.mark.parametrize('number', ['1e999', '-1e999'])
def test_shared_reader_rejects_overflow(tmp_path, number):
    path = tmp_path / 'overflow.json'
    path.write_text('{"meta": {"SECRET": ' + number + '}}')
    with pytest.raises(ManifestError, match='Non-finite JSON number'):
        read_json_artifact(path, 'diff report')


def column():
    return {'name': 'id', 'description': '', 'data_type': None, 'meta': {},
            'constraints': [], 'quote': None, 'tags': [], '_extra': {},
            'granularity': None, 'config': {'meta': {}, 'tags': [], '_extra': {}}}


def changed(kind):
    event = {'kind': kind, 'node_id': 'SECRET.node', 'before_file': None, 'after_file': None}
    if kind == 'dependencies_changed':
        event.update(added=[{'node_id': 'SECRET.upstream', 'file': None}], removed=[])
    if kind.startswith('column_'):
        event.update(column='SECRET.column', before=None if kind == 'column_added' else column(),
                     after=None if kind == 'column_removed' else column())
    data = unchanged()
    data.update(has_changes=True, changes=[event])
    data['summary'][kind] = 1
    return data


def check(tmp_path, capsys, data, valid):
    path = tmp_path / 'SECRET-diff.json'
    content = data if isinstance(data, bytes) else json.dumps(data).encode()
    path.write_bytes(content)
    assert main(['validate-diff', str(path)]) == (0 if valid else 1)
    assert capsys.readouterr() == ((SUCCESS, '') if valid else ('', FAILURE))
    assert path.read_bytes() == content


@pytest.mark.parametrize('kind', [*KINDS, 'future_kind'])
def test_all_kinds_with_null_provenance(tmp_path, capsys, kind):
    check(tmp_path, capsys, changed(kind), True)


def test_additive_and_structural_limits(tmp_path, capsys):
    data = changed('column_changed')
    data.update(future_field={'SECRET': [True, None, 1]}, diff_version=1.0)
    data['summary'].update(column_changed=99, future_kind=2)
    event = data['changes'][0]
    event['after']['meta'] = {'SECRET': {'nested': [False, 3.25, 'value']}}
    event['before'] = deepcopy(event['after'])  # Equality intentionally allowed.
    event['future_field'] = {'SECRET': None}
    data['changes'].append(deepcopy(event))  # Duplicates intentionally allowed.
    data['changes'].insert(0, changed('future_kind')['changes'][0])
    dependency = changed('dependencies_changed')['changes'][0]
    dependency['removed'] = deepcopy(dependency['added'])  # Overlap allowed.
    data['changes'].append(dependency)
    # No relationship to original manifests or supplied counter totals is checked.
    check(tmp_path, capsys, data, True)
    empty = unchanged()
    empty.update(future_field=[], summary={**empty['summary'], 'future_kind': 0.0})
    check(tmp_path, capsys, empty, True)


@pytest.mark.parametrize('field,value', [
    ('diff_version', None), ('diff_version', True), ('diff_version', '1'), ('diff_version', 2),
    ('has_changes', 'false'), ('has_changes', True), ('summary', {}), ('summary', []),
    ('changes', {}), ('changes', [changed('model_added')['changes'][0]]),
])
def test_invalid_envelope(tmp_path, capsys, field, value):
    data = unchanged()
    data[field] = value
    check(tmp_path, capsys, data, False)


@pytest.mark.parametrize('problem', [
    'missing_provenance', 'invalid_provenance', 'wrong_absent_side', 'lifecycle_payload',
    'missing_dependency_file', 'empty_dependencies', 'missing_column', 'missing_metadata',
    'invalid_metadata', 'secret_counter', 'unchanged_counter', 'known_kind_fallback',
    'future_missing_provenance',
])
def test_invalid_shapes_do_not_echo_keys_or_values(tmp_path, capsys, problem):
    data = changed('column_changed')
    event = data['changes'][0]
    if problem == 'missing_provenance':
        del event['before_file']
    elif problem == 'invalid_provenance':
        event['after_file'] = {'SECRET': 'SECRET'}
    elif problem in ('wrong_absent_side', 'lifecycle_payload', 'known_kind_fallback'):
        data = changed('source_added')
        event = data['changes'][0]
        event.update({'before_file': 'SECRET.yaml'} if problem == 'wrong_absent_side'
                     else {'column': 'SECRET'} if problem == 'lifecycle_payload' else {'added': []})
    elif problem in ('missing_dependency_file', 'empty_dependencies'):
        data = changed('dependencies_changed')
        if problem == 'empty_dependencies':
            data['changes'][0]['added'] = []
        else:
            del data['changes'][0]['added'][0]['file']
    elif problem == 'missing_column':
        del event['column']
    elif problem == 'missing_metadata':
        del event['after']['config']
    elif problem == 'invalid_metadata':
        event['after']['tags'] = ['SECRET', 'SECRET']
    elif problem == 'secret_counter':
        data['summary']['SECRET\n' * 1000] = -1
    elif problem == 'unchanged_counter':
        data = unchanged()
        data['summary']['source_added'] = 1
    else:
        data = changed('future_kind')
        del data['changes'][0]['after_file']
    check(tmp_path, capsys, data, False)


@pytest.mark.parametrize('content', [
    b'{"SECRET":', b'null', b'[]', b'"SECRET"', b'\xff',
    b'{"SECRET":1,"SECRET":2}', b'{"nested":{"SECRET":1,"SECRET":2}}',
    b'{"SECRET":NaN}', b'{"SECRET":Infinity}', b'{"SECRET":-Infinity}',
    b'{"SECRET":1e999}', b'{"SECRET":-1e999}', b'[' * 2000,
])
def test_invalid_json(tmp_path, capsys, content):
    check(tmp_path, capsys, content, False)


@pytest.mark.parametrize('problem', ['missing', 'directory', 'oversized', 'permission'])
def test_unreadable_and_oversized(tmp_path, capsys, monkeypatch, problem):
    from pathlib import Path
    from datawrench.sqlmesh_manifest import validation

    path = tmp_path / 'SECRET.json'
    if problem == 'directory':
        path.mkdir()
    elif problem in ('oversized', 'permission'):
        path.write_text(json.dumps(unchanged()))
        if problem == 'oversized':
            monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', 8)
        else:
            def denied(*args, **kwargs):
                raise PermissionError('SECRET')
            monkeypatch.setattr(Path, 'open', denied)
    assert main(['validate-diff', str(path)]) == 1
    assert capsys.readouterr() == ('', FAILURE)


@pytest.mark.parametrize('args', [[], ['x', 'extra'], ['x', '--format', 'yaml'],
                                  ['x', '--out', 'y'], ['--unknown']])
def test_argument_errors(args, capsys):
    with pytest.raises(SystemExit) as exc:
        main(['validate-diff', *args])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == ''
    assert 'usage:' in err and 'error:' in err


def test_offline_and_no_project_or_manifest_validation(tmp_path, capsys, monkeypatch):
    import socket
    import requests
    from datawrench.sqlmesh_manifest import cli, validation

    def blocked(*args, **kwargs):
        pytest.fail('validate-diff must only read JSON and the packaged schema')

    for name in ('load_project', 'build_manifest', 'validate_manifest', 'read_manifest'):
        monkeypatch.setattr(cli, name, blocked)
    monkeypatch.setattr(validation, 'parse_manifest', blocked)
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    data = changed('future_kind')
    data['$schema'] = 'https://SECRET.invalid/schema.json'
    data['$ref'] = 'https://SECRET.invalid/payload.json'
    check(tmp_path, capsys, data, True)


def test_unexpected_error_is_bounded(tmp_path, capsys, monkeypatch):
    from datawrench.sqlmesh_manifest import cli

    def broken(*args, **kwargs):
        raise RuntimeError('SECRET' * 10000)

    monkeypatch.setattr(cli, 'validate_diff', broken)
    check(tmp_path, capsys, unchanged(), False)


@pytest.mark.parametrize('number', ['1.0', '1.5', '1e100', '-1e-100', '1e-999'])
def test_shared_reader_preserves_finite_numbers(tmp_path, number):
    path = tmp_path / 'finite.json'
    path.write_text('{"value":' + number + '}')
    data, content = read_json_artifact(path, 'diff report')
    assert data == {'value': float(number)}
    assert content == path.read_bytes()
