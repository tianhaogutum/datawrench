"""Report-position stdin keeps exact-byte manifest and provenance checks."""
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
from datawrench.sqlmesh_manifest.report_validation import validate_report

from .test_report_validation import entry, pair  # shared exported report/manifest fixture

TEXT = b'Valid provenance report v1 for supplied manifest\n'


def test_piped_report_regression(pair, tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}
    result = subprocess.run(
        [sys.executable, '-m', 'datawrench', 'sqlmesh-manifest', 'validate-report', '-',
         '--manifest', str(pair[0])], input=pair[1].read_bytes(),
        capture_output=True, cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    assert (result.stdout, result.stderr) == (TEXT, b'')


MODES = ([], ['--format', 'text'], ['--format', 'json'])
COUNTS = dict(resources=7, models=3, seeds=1, sources=1, tests=2, dependencies=5)


def feed(monkeypatch, content):
    stream = io.BytesIO(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    return stream


def check(pair, capsys, mode, *, rule=None, input='report', path='$', report='-', manifest=None):
    code = main(['validate-report', report, '--manifest', str(manifest or pair[0]), *mode])
    assert code == (1 if rule else 0)
    out, err = capsys.readouterr()
    if mode == ['--format', 'json']:
        assert (out, err) == (serialize({
            'validation_version': 1, 'valid': rule is None,
            'report_version': None if rule else 1, 'counts': None if rule else COUNTS,
            'diagnostics': [] if rule is None else [dict(input=input, path=path, rule=rule)],
        }), '')
    elif rule:
        assert out == '' and err.startswith('validation error: ')
        assert 'SECRET' not in err and len(err.encode()) < 2048
    else:
        assert (out.encode(), err) == (TEXT, '')
    return out, err


@pytest.mark.parametrize('mode', MODES)
def test_matching_pair_and_file_bytes(pair, monkeypatch, capsys, mode):
    before = [p.read_bytes() for p in pair]
    expected = check(pair, capsys, mode, report=str(pair[1]))
    stream = feed(monkeypatch, before[1])
    assert check(pair, capsys, mode) == expected
    assert not stream.closed and stream.tell() == len(before[1])
    assert [p.read_bytes() for p in pair] == before


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('content', [
    b'', b' \n\t', b'{"SECRET":', b'{}{}', b'null', b'[]', b'"SECRET"', b'42', b'\xff',
    b'{"SECRET":1,"SECRET":2}', b'{"nested":{"SECRET":1,"SECRET":2}}',
    b'{"SECRET":NaN}', b'{"SECRET":Infinity}', b'{"SECRET":-Infinity}',
    b'{"SECRET":1e999}', b'{"SECRET":-1e999}', b'[' * 2000,
])
def test_invalid_streams(pair, monkeypatch, capsys, mode, content):
    stream = feed(monkeypatch, content)
    check(pair, capsys, mode, rule='read_json')
    assert not stream.closed


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('version', [None, True, 1.0, 'SECRET', 0, 2])
def test_unsupported_versions(pair, monkeypatch, capsys, mode, version):
    data = json.loads(pair[1].read_bytes()); data['report_version'] = version
    feed(monkeypatch, serialize(data).encode())
    check(pair, capsys, mode, rule='report_version', path='$.report_version')


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('change,rule,path,input', [
    (lambda d: d.update(manifest_sha256='0' * 64), 'manifest_sha256', '$.manifest_sha256', 'pair'),
    (lambda d: d.update(project_name='SECRET'), 'project_name', '$.project_name', 'pair'),
    (lambda d: d['resources'].pop(), 'resources', '$.resources', 'pair'),
    (lambda d: d['resources'].append(copy.deepcopy(d['resources'][0])), 'duplicate_resources', '$.resources', 'report'),
    (lambda d: entry(d)['origin'].update(file='SECRET'), 'origin', '.origin', 'pair'),
    (lambda d: entry(d)['dependencies'].clear(), 'dependencies', '.dependencies', 'pair'),
    (lambda d: entry(d)['dependencies'].append(copy.deepcopy(entry(d)['dependencies'][0])), 'duplicate_dependencies', '.dependencies', 'report'),
    (lambda d: entry(d)['dependencies'][0].update(file='SECRET'), 'dependency_provenance', '.dependencies[0]', 'pair'),
    (lambda d: entry(d).pop('origin'), 'schema.required', '', 'report'),
])
def test_report_consistency_stages(pair, monkeypatch, capsys, mode, change, rule, path, input):
    data = json.loads(pair[1].read_bytes())
    index = data['resources'].index(entry(data))
    change(data)
    feed(monkeypatch, serialize(data).encode())
    if not path.startswith('$'):
        path = f'$.resources[{index}]' + path
    check(pair, capsys, mode, rule=rule, path=path, input=input)


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('problem', ['stale', 'missing', 'malformed', 'version', 'graph'])
def test_manifest_file_failure_stages(pair, monkeypatch, capsys, mode, problem):
    data = json.loads(pair[0].read_bytes())
    rule, path, input = 'manifest_validation', '$', 'manifest'
    if problem == 'stale':
        pair[0].write_bytes(pair[0].read_bytes() + b' ')
        rule, path, input = 'manifest_sha256', '$.manifest_sha256', 'pair'
    elif problem == 'missing':
        pair[0].unlink(); rule = 'read_json'
    elif problem == 'malformed':
        pair[0].write_bytes(b'{'); rule = 'read_json'
    else:
        if problem == 'version':
            data['metadata']['dbt_schema_version'] = 'https://SECRET.invalid/v13.json'
            rule, path = 'manifest_version', '$.metadata.dbt_schema_version'
        else:
            next(n for n in data['nodes'].values() if n['resource_type'] == 'model')['depends_on']['nodes'] = ['model.SECRET']
            data['parent_map'] = data['child_map'] = {}
        pair[0].write_text(serialize(data))
    feed(monkeypatch, pair[1].read_bytes())
    check(pair, capsys, mode, rule=rule, path=path, input=input)


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('failure', ['oserror', 'closed', 'missing', 'text', 'unexpected'])
def test_read_failures(pair, monkeypatch, capsys, mode, failure):
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
    stdin = None if failure == 'missing' else io.StringIO('SECRET') if failure == 'text' else SimpleNamespace(buffer=stream)
    monkeypatch.setattr(sys, 'stdin', stdin)
    if failure == 'unexpected' and mode != ['--format', 'json']:
        assert main(['validate-report', '-', '--manifest', str(pair[0]), *mode]) == 1
        out, err = capsys.readouterr()
        assert out == '' and 'SECRET' not in err and len(err) < 256
    else:
        check(pair, capsys, mode, rule='internal_error' if failure == 'unexpected' else 'read_json',
              input='pair' if failure == 'unexpected' else 'report')


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('extra', [0, 1, 2])
def test_exact_limit_and_bounded_read(pair, monkeypatch, capsys, mode, extra):
    from datawrench.sqlmesh_manifest import validation
    assert validation.MAX_MANIFEST_BYTES == 64 * 1024 * 1024
    content = pair[1].read_bytes()
    limit = max(p.stat().st_size for p in pair) + 8
    content += b' ' * (limit - len(content) + extra)
    monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', limit)
    class Observed(io.BytesIO):
        def read(self, size=-1):
            assert size == limit + 1
            return super().read(size)
    stream = Observed(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    check(pair, capsys, mode, rule='read_json' if extra else None)
    assert stream.tell() == min(len(content), limit + 1) and not stream.closed


@pytest.mark.parametrize('mode', MODES)
def test_dash_literal_files_and_manifest_file_semantics(pair, tmp_path, monkeypatch, capsys, mode):
    monkeypatch.chdir(tmp_path)
    literal = tmp_path / '-'; literal.write_bytes(pair[1].read_bytes())
    stream = feed(monkeypatch, b'')
    check(pair, capsys, mode, rule='read_json')  # No fallback to existing report dash.
    check(pair, capsys, mode, report='./-')
    check(pair, capsys, mode, report=str(literal))
    assert validate_report(Path('-'), pair[0]) == COUNTS  # File-only Path API.
    assert stream.tell() == 0
    literal.write_bytes(pair[0].read_bytes())
    feed(monkeypatch, pair[1].read_bytes())
    check(pair, capsys, mode, manifest='-')  # Both dashes; manifest stays a file.
    literal.write_bytes(b'{')
    feed(monkeypatch, pair[1].read_bytes())
    check(pair, capsys, mode, manifest='-', rule='read_json', input='manifest')
    feed(monkeypatch, pair[1].read_bytes())
    check(pair, capsys, mode, report='./-', rule='read_json')
    assert literal.read_bytes() == b'{'


@pytest.mark.parametrize('args', [[], ['-', '--format', 'json'], ['-', '--manifest'],
    ['-', '--manifest', 'file', '--format', 'yaml'], ['-', '--manifest', 'file', '--format'],
    ['-', '--manifest', 'file', 'extra'], ['-', '--manifest', 'file', '--out', 'SECRET'],
    ['-', '--manifest', 'file', '--unknown']])
def test_argument_errors_do_not_read(monkeypatch, capsys, args):
    class Unreadable:
        @property
        def buffer(self):
            pytest.fail('Argument errors must not read stdin')
    monkeypatch.setattr(sys, 'stdin', Unreadable())
    with pytest.raises(SystemExit) as exc:
        main(['validate-report', *args])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == '' and 'usage:' in err and 'error:' in err


@pytest.mark.parametrize('mode', MODES)
def test_offline_read_only(pair, monkeypatch, capsys, mode):
    import socket
    import requests
    from datawrench.sqlmesh_manifest import cli, report_validation
    def blocked(*args, **kwargs):
        pytest.fail('No project, schema fetching or artifact writes permitted')
    for name in ('load_project', 'build_manifest', '_write_atomic'):
        monkeypatch.setattr(cli, name, blocked)
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    read_file = report_validation.read_json_artifact
    reads = []
    def only_manifest(path, kind):
        assert Path(path) == pair[0] and kind == 'manifest'
        reads.append(kind)
        return read_file(path, kind)
    monkeypatch.setattr(report_validation, 'read_json_artifact', only_manifest)
    before = [p.read_bytes() for p in pair]
    stream = feed(monkeypatch, before[1])
    check(pair, capsys, mode)
    assert reads == ['manifest'] and not stream.closed
    assert [p.read_bytes() for p in pair] == before
