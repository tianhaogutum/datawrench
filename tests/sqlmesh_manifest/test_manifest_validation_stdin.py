"""Standalone manifest stdin selection and existing validation contracts."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.exporter import serialize
from .test_manifest_validation_json import LEGACY, TEXT

MODES = ([], ['--format', 'text'], ['--format', 'json'])


def test_piped_input_regression(tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}
    r = subprocess.run([sys.executable, '-m', 'datawrench', 'sqlmesh-manifest', 'validate', '-'],
                       input=LEGACY.read_bytes(), capture_output=True, cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    assert (r.stdout, r.stderr) == (TEXT.encode(), b'')


def feed(monkeypatch, content):
    stream = io.BytesIO(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    return stream


def check(capsys, mode, *, rule=None, version=12, counts=None, file='-'):
    assert main(['validate', file, *mode]) == (1 if rule else 0)
    out, err = capsys.readouterr()
    if mode == ['--format', 'json']:
        data = {'manifest_validation_version': 1, 'valid': rule is None,
                'manifest_version': None if rule else version, 'counts': None if rule else counts,
                'diagnostics': [] if rule is None else [{
                    'path': '$.metadata.dbt_schema_version' if rule == 'manifest_version' else '$', 'rule': rule}]}
        assert (out, err) == (serialize(data), '')
    elif rule:
        assert out == '' and err.startswith('validation error: ')
        assert len(err.encode()) < 2048 and 'SECRET' not in err
    else:
        assert (out, err) == (TEXT, '')


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('version', range(1, 13))
def test_supported_versions(monkeypatch, capsys, example, mode, version):
    data = copy.deepcopy(example[1])
    for key in ('nodes', 'sources', 'macros', 'docs', 'exposures', 'metrics', 'groups',
                'saved_queries', 'semantic_models', 'unit_tests', 'parent_map', 'child_map'):
        data[key] = {}
    if version < 12:
        data = json.loads(LEGACY.read_text())
        data['docs'] = {}
        if version >= 4:
            data['metrics'] = {}
        if version >= 9:
            data.update(groups={}, group_map={})
        if version >= 10:
            data['semantic_models'] = {}
        if version >= 11:
            data.update(disabled={}, parent_map={}, child_map={}, saved_queries={})
    data['metadata']['dbt_schema_version'] = f'https://schemas.getdbt.com/dbt/manifest/v{version}.json'
    stream = feed(monkeypatch, serialize(data).encode())
    check(capsys, mode, version=version,
          counts=dict(resources=0, models=0, seeds=0, sources=0, tests=0, edges=0))
    assert not stream.closed


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('parents', ['normal', 'null'])
def test_v12_resource_counts(monkeypatch, capsys, example, mode, parents):
    data = copy.deepcopy(example[1])
    if parents == 'null':
        data['parent_map'] = None
    stream = feed(monkeypatch, serialize(data).encode())
    check(capsys, mode, counts=dict(resources=7, models=3, seeds=1, sources=1, tests=2,
                                   edges=5 if parents == 'normal' else 0))
    assert not stream.closed


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('content', [
    b'', b' \n\t', b'{"SECRET":', b'{}{}', b'null', b'[]', b'"SECRET"', b'42', b'\xff',
    b'{"SECRET":1,"SECRET":2}', b'{"nested":{"SECRET":1,"SECRET":2}}',
    b'{"SECRET":NaN}', b'{"SECRET":Infinity}', b'{"SECRET":-Infinity}',
    b'{"SECRET":1e999}', b'{"SECRET":-1e999}', b'[' * 2000,
])
def test_invalid_streams(monkeypatch, capsys, mode, content):
    stream = feed(monkeypatch, content)
    check(capsys, mode, rule='read_json')
    assert not stream.closed


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('problem', ['version', 'schema', 'parser', 'dangling', 'id', 'reciprocal'])
def test_existing_validation_stages(monkeypatch, capsys, example, mode, problem):
    data = copy.deepcopy(example[1])
    rule = 'graph_validation'
    if problem == 'version':
        data['metadata']['dbt_schema_version'] = 'https://SECRET.invalid/v13.json'
        rule = 'manifest_version'
    elif problem == 'schema':
        data['nodes']['SECRET'] = {'SECRET': 'SECRET'}
        rule = 'schema_validation'
    elif problem == 'parser':
        data = json.loads(LEGACY.read_text())
        data['docs']['SECRET'] = {'SECRET': 'SECRET'}
        rule = 'parser_validation'
    elif problem == 'dangling':
        data['nodes']['model.mini_project.stg_orders']['depends_on']['nodes'] = ['model.SECRET']
        data['parent_map'] = data['child_map'] = {}
    elif problem == 'id':
        data['nodes']['model.mini_project.stg_orders']['unique_id'] = 'model.SECRET'
    else:
        data['parent_map']['seed.mini_project.raw_orders'] = [next(iter(data['sources']))]
    feed(monkeypatch, serialize(data).encode())
    # JSON hides keys; existing bounded text schema/parser paths may contain keys.
    if rule in ('schema_validation', 'parser_validation') and mode != ['--format', 'json']:
        assert main(['validate', '-', *mode]) == 1
        out, err = capsys.readouterr()
        assert out == '' and err.startswith('validation error: ') and len(err.encode()) < 2048
    else:
        check(capsys, mode, rule=rule)


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('failure', ['oserror', 'closed', 'missing', 'unexpected'])
def test_read_failures(monkeypatch, capsys, mode, failure):
    class Broken:
        def read(self, size):
            raise OSError('SECRET input path')
    stream = Broken()
    if failure == 'closed':
        stream = io.BytesIO(); stream.close()
    if failure == 'unexpected':
        class Unexpected:
            def read(self, size):
                raise RuntimeError('SECRET input path')
        stream = Unexpected()
    monkeypatch.setattr(sys, 'stdin', None if failure == 'missing' else SimpleNamespace(buffer=stream))
    if failure == 'unexpected' and mode != ['--format', 'json']:
        assert main(['validate', '-', *mode]) == 1
        out, err = capsys.readouterr()
        assert out == '' and 'SECRET' not in err and len(err) < 256
    else:
        check(capsys, mode, rule='internal_error' if failure == 'unexpected' else 'read_json')


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('extra', [0, 1, 2])
def test_exact_limit_and_bounded_read(monkeypatch, capsys, mode, extra):
    from datawrench.sqlmesh_manifest import validation
    assert validation.MAX_MANIFEST_BYTES == 64 * 1024 * 1024
    content = LEGACY.read_bytes()
    limit = len(content) + 8
    content += b' ' * (limit - len(content) + extra)
    monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', limit)
    class Observed(io.BytesIO):
        def read(self, size=-1):
            assert size == limit + 1
            return super().read(size)
    stream = Observed(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    check(capsys, mode, rule='read_json' if extra else None, version=1,
          counts=dict(resources=1, models=0, seeds=0, sources=0, tests=0, edges=0))
    assert stream.tell() == min(len(content), limit + 1) and not stream.closed


@pytest.mark.parametrize('mode', MODES)
def test_dash_and_literal_files(monkeypatch, capsys, tmp_path, mode):
    from datawrench.sqlmesh_manifest.validation import read_manifest
    monkeypatch.chdir(tmp_path)
    literal = tmp_path / '-'
    literal.write_bytes(LEGACY.read_bytes())
    stream = feed(monkeypatch, b'')
    check(capsys, mode, rule='read_json')  # No file fallback.
    counts = dict(resources=1, models=0, seeds=0, sources=0, tests=0, edges=0)
    check(capsys, mode, version=1, counts=counts, file='./-')
    check(capsys, mode, version=1, counts=counts, file=str(literal))
    assert read_manifest('-') == json.loads(LEGACY.read_bytes())  # File API unchanged.
    assert stream.tell() == 0
    literal.write_bytes(b'{')
    feed(monkeypatch, LEGACY.read_bytes())
    check(capsys, mode, version=1, counts=counts)
    check(capsys, mode, rule='read_json', file='./-')
    assert literal.read_bytes() == b'{'


@pytest.mark.parametrize('args', [[], ['--format', 'json'], ['-', '--format', 'yaml'],
    ['-', '--format'], ['-', 'extra', '--format', 'json'], ['-', '--out', 'SECRET'], ['-', '--unknown']])
def test_argument_errors_do_not_read(monkeypatch, capsys, args):
    class Unreadable:
        @property
        def buffer(self):
            pytest.fail('Argument errors must not read stdin')
    monkeypatch.setattr(sys, 'stdin', Unreadable())
    with pytest.raises(SystemExit) as exc:
        main(['validate', *args])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == '' and 'error:' in err and 'usage:' in err


@pytest.mark.parametrize('mode', MODES)
def test_offline_read_only(monkeypatch, capsys, example, mode):
    import requests
    import socket
    from datawrench.sqlmesh_manifest import cli, validation
    def blocked(*args, **kwargs):
        pytest.fail('No project, network, artifact input/output permitted')
    for name in ('load_project', 'build_manifest', '_write_atomic'):
        monkeypatch.setattr(cli, name, blocked)
    monkeypatch.setattr(validation, 'read_json_artifact', blocked)
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    data = copy.deepcopy(example[1])
    data['nodes']['model.mini_project.stg_orders']['meta'] = {
        '$schema': 'https://SECRET.invalid/schema', '$ref': 'https://SECRET.invalid/data'}
    stream = feed(monkeypatch, serialize(data).encode())
    check(capsys, mode, counts=dict(resources=7, models=3, seeds=1, sources=1, tests=2, edges=5))
    assert not stream.closed
