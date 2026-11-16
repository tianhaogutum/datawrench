"""Summary stdin reuses saved-manifest validation and text/count semantics."""
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
from datawrench.sqlmesh_manifest.validation import read_manifest
from .test_manifest_validation_json import LEGACY

from datawrench.sqlmesh_manifest.exporter import serialize


TEXT = b'models=3 seeds=1 sources=1 tests=2 edges=5\n'


def test_piped_manifest_regression(example, tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'sqlmesh-manifest', 'summary', '-'],
                            input=serialize(example[1]).encode(), capture_output=True,
                            cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    assert (result.stdout, result.stderr) == (TEXT, b'')

LEGACY_SUMMARY = LEGACY.with_name('v1-summary.json')
LEGACY_TEXT = 'models=0 seeds=0 sources=0 tests=0 analyses=1 macros=1 edges=0\n'
EMPTY_TEXT = 'models=0 seeds=0 sources=0 tests=0 edges=0\n'


def feed(monkeypatch, content):
    stream = io.BytesIO(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    return stream


def check(capsys, *, text=TEXT.decode(), error=None, file='-'):
    assert main(['summary', file]) == (1 if error else 0)
    out, err = capsys.readouterr()
    if error:
        assert out == '' and err.startswith(error)
        assert len(err.encode()) < 2048
    else:
        assert (out, err) == (text, '')
    return err


@pytest.mark.parametrize('version', range(1, 13))
def test_supported_versions(monkeypatch, capsys, example, version):
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
    check(capsys, text=EMPTY_TEXT)
    assert not stream.closed


@pytest.mark.parametrize('maps', ['absent', 'null', 'empty'])
def test_nonempty_legacy_extra_resource_labels(monkeypatch, capsys, maps):
    data = json.loads(LEGACY_SUMMARY.read_text())
    if maps != 'absent':
        data['parent_map'] = data['child_map'] = None if maps == 'null' else {}
    feed(monkeypatch, serialize(data).encode())
    check(capsys, text=LEGACY_TEXT)


@pytest.mark.parametrize('parents', ['normal', 'null', 'both-null'])
def test_v12_counts_parent_map_only(monkeypatch, capsys, example, parents):
    data = copy.deepcopy(example[1])
    if parents != 'normal':
        data['parent_map'] = None
    if parents == 'both-null':
        data['child_map'] = None
    feed(monkeypatch, serialize(data).encode())
    expected = TEXT.decode() if parents == 'normal' else TEXT.decode().replace('edges=5', 'edges=0')
    check(capsys, text=expected)


def test_v12_extra_resources_and_disabled_exclusion(monkeypatch, capsys, example):
    data = copy.deepcopy(example[1])
    data['macros']['macro.mini_project.example'] = {
        'unique_id': 'macro.mini_project.example', 'resource_type': 'macro',
        'name': 'example', 'package_name': 'mini_project', 'path': 'example.sql',
        'original_file_path': 'macros/example.sql', 'macro_sql': '{% macro example() %}1{% endmacro %}'}
    uid, node = next(iter(data['nodes'].items()))
    data['disabled'] = {uid: [copy.deepcopy(node)]}
    feed(monkeypatch, serialize(data).encode())
    check(capsys, text=TEXT.decode().replace('edges=5', 'macros=1 edges=5'))


@pytest.mark.parametrize('content', [
    b'', b' \n\t', b'{"SECRET":', b'{}{}', b'null', b'[]', b'"SECRET"', b'42', b'\xff',
    b'{"SECRET":1,"SECRET":2}', b'{"nested":{"SECRET":1,"SECRET":2}}',
    b'{"SECRET":NaN}', b'{"SECRET":Infinity}', b'{"SECRET":-Infinity}',
    b'{"SECRET":1e999}', b'{"SECRET":-1e999}', b'[' * 2000,
])
def test_invalid_streams(monkeypatch, capsys, content):
    stream = feed(monkeypatch, content)
    assert 'SECRET' not in check(capsys, error='validation error: ')
    assert not stream.closed


@pytest.mark.parametrize('problem', ['version', 'schema', 'parser', 'dangling', 'id',
                                    'duplicates', 'omitted', 'reciprocal'])
def test_validation_stages(monkeypatch, capsys, example, problem):
    data = copy.deepcopy(example[1])
    expected = 'validation error: '
    if problem == 'version':
        data['metadata']['dbt_schema_version'] = 'https://SECRET.invalid/v13.json'
        expected += 'Unsupported or invalid dbt manifest schema'
    elif problem == 'schema':
        data['nodes']['SECRET'] = {'SECRET': 'SECRET'}
        expected += '$.nodes.SECRET: schema rule'
    elif problem == 'parser':
        data = json.loads(LEGACY.read_text())
        data['docs']['SECRET'] = {'SECRET': 'SECRET'}
    elif problem == 'dangling':
        data['nodes']['model.mini_project.stg_orders']['depends_on']['nodes'] = ['model.SECRET']
        data['parent_map'] = data['child_map'] = {}
        expected += 'depends_on.nodes contains a dangling dependency'
    elif problem == 'id':
        data['nodes']['model.mini_project.stg_orders']['unique_id'] = 'model.SECRET'
        expected += 'Resource key does not match its unique_id'
    elif problem == 'duplicates':
        data['nodes']['model.mini_project.stg_orders']['depends_on']['nodes'] *= 2
        expected += 'depends_on.nodes contains duplicate edges'
    elif problem == 'omitted':
        data['parent_map'] = {}
        data['child_map'] = None
        expected += 'depends_on.nodes differs from parent_map'
    else:
        data['parent_map']['seed.mini_project.raw_orders'] = [next(iter(data['sources']))]
        expected += 'parent_map contains a non-reciprocal edge'
    feed(monkeypatch, serialize(data).encode())
    check(capsys, error=expected)


@pytest.mark.parametrize('failure', ['oserror', 'closed', 'missing', 'text-only', 'unexpected'])
def test_read_failures(monkeypatch, capsys, failure):
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
    stdin = SimpleNamespace(buffer=stream)
    if failure == 'missing':
        stdin = None
    elif failure == 'text-only':
        stdin = io.StringIO('SECRET')
    monkeypatch.setattr(sys, 'stdin', stdin)
    assert main(['summary', '-']) == 1
    out, err = capsys.readouterr()
    assert out == '' and 'SECRET' not in err and len(err) < 256
    assert err.startswith('error: RuntimeError' if failure == 'unexpected' else 'validation error: Cannot read manifest')


@pytest.mark.parametrize('extra', [0, 1, 2])
def test_exact_limit_and_bounded_read(monkeypatch, capsys, extra):
    from datawrench.sqlmesh_manifest import validation
    assert validation.MAX_MANIFEST_BYTES == 64 * 1024 * 1024
    content = LEGACY_SUMMARY.read_bytes()
    limit = len(content) + 8
    content += b' ' * (limit - len(content) + extra)
    monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', limit)
    calls = []
    class Observed(io.BytesIO):
        def read(self, size=-1):
            calls.append(size)
            return super().read(size)
        def write(self, content):
            pytest.fail('Do not write stdin')
    stream = Observed(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    check(capsys, text=LEGACY_TEXT,
          error='validation error: Manifest exceeds the 64 MiB input limit' if extra else None)
    assert calls == [limit + 1]
    assert stream.tell() == min(len(content), limit + 1) and not stream.closed


def test_literal_files_and_no_fallback(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    literal = tmp_path / '-'
    literal.write_bytes(LEGACY_SUMMARY.read_bytes())
    stream = feed(monkeypatch, b'')
    check(capsys, error='validation error: Invalid or truncated JSON manifest')
    class Unreadable:
        @property
        def buffer(self):
            pytest.fail('File input must not read stdin')
    monkeypatch.setattr(sys, 'stdin', Unreadable())
    check(capsys, text=LEGACY_TEXT, file='./-')
    check(capsys, text=LEGACY_TEXT, file=str(literal))
    check(capsys, error='validation error: Cannot read manifest', file='./missing')
    assert read_manifest('-') == json.loads(LEGACY_SUMMARY.read_bytes())
    assert stream.tell() == 0 and not stream.closed
    literal.write_bytes(b'{')
    stream = feed(monkeypatch, LEGACY_SUMMARY.read_bytes())
    check(capsys, error='validation error: Invalid or truncated JSON manifest', file='./-')
    assert stream.tell() == 0
    check(capsys, text=LEGACY_TEXT)
    assert literal.read_bytes() == b'{'


@pytest.mark.parametrize('args', [[], ['-', '--format', 'json'], ['-', '--format'],
    ['-', 'extra'], ['-', '--out', 'SECRET'], ['-', '--unknown']])
def test_argument_errors_before_stdin(monkeypatch, capsys, args):
    class Unreadable:
        @property
        def buffer(self):
            pytest.fail('Argument errors must not read stdin')
    monkeypatch.setattr(sys, 'stdin', Unreadable())
    with pytest.raises(SystemExit) as exc:
        main(['summary', *args])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == '' and 'error:' in err and 'usage:' in err


def test_offline_read_only(monkeypatch, capsys, example):
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
    content = serialize(data).encode()
    stream = feed(monkeypatch, content)
    check(capsys)
    assert stream.getvalue() == content and not stream.closed
