"""CLI stdin shares the bounded saved-diff validation contract."""
import json
import io
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
from .test_diff_validation import FAILURE, KINDS, SUCCESS, changed, unchanged

MODES = ([], ['--format', 'text'], ['--format', 'json'])


def check(capsys, mode, valid, file='-'):
    assert main(['validate-diff', file, *mode]) == (0 if valid else 1)
    if mode == ['--format', 'json']:
        result = {'diff_validation_version': 1, 'valid': valid,
                  'diagnostics': [] if valid else [{'path': '$', 'rule': 'invalid_diff'}]}
        assert capsys.readouterr() == (serialize(result), '')
    else:
        assert capsys.readouterr() == ((SUCCESS, '') if valid else ('', FAILURE))


def feed(monkeypatch, content):
    stream = io.BytesIO(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    return stream


def test_piped_input_regression(tmp_path):
    # Real OS pipe, with no file named '-' and no checkout working directory.
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}
    result = subprocess.run(
        [sys.executable, '-m', 'datawrench', 'sqlmesh-manifest', 'validate-diff', '-'],
        input=json.dumps(unchanged()).encode(), capture_output=True, cwd=tmp_path, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == SUCCESS.encode()
    assert result.stderr == b''


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('kind', [None, *KINDS, 'description_changed', 'future_kind'])
def test_original_description_and_additive(monkeypatch, capsys, mode, kind):
    data = unchanged() if kind is None else changed(kind)
    if kind == 'description_changed':
        data['changes'][0].update(before='旧\r\nSECRET', after='新\n🐍')
    data.update(SECRET={'arbitrary': [None, True, 1.5]}, diff_version=1.0)
    if kind is not None:
        data['changes'] *= 2  # No new event uniqueness or counter arithmetic.
        data['summary']['future_counter'] = 99
    stream = feed(monkeypatch, json.dumps(data, ensure_ascii=False).encode())
    check(capsys, mode, True)
    assert not stream.closed


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('content', [
    b'', b' \n\t', b'{"SECRET":', b'{}{}', b'null', b'[]', b'"SECRET"', b'42',
    b'\xff', b'{"SECRET":1,"SECRET":2}', b'{"nested":{"SECRET":1,"SECRET":2}}',
    b'{"SECRET":NaN}', b'{"SECRET":Infinity}', b'{"SECRET":-Infinity}',
    b'{"SECRET":1e999}', b'{"SECRET":-1e999}', b'[' * 2000,
    json.dumps({**unchanged(), 'diff_version': 2}).encode(),
    json.dumps(changed('description_changed')).encode(),  # Missing strings.
])
def test_invalid_streams(monkeypatch, capsys, mode, content):
    stream = feed(monkeypatch, content)
    check(capsys, mode, False)
    assert not stream.closed


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('exception', [OSError('SECRET'), ValueError('SECRET'), RuntimeError('SECRET')])
def test_read_failures(monkeypatch, capsys, mode, exception):
    class Broken:
        def read(self, size):
            raise exception

    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=Broken()))
    check(capsys, mode, False)


@pytest.mark.parametrize('mode', MODES)
def test_unavailable_binary_stdin(monkeypatch, capsys, mode):
    monkeypatch.setattr(sys, 'stdin', None)
    check(capsys, mode, False)


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('extra', [0, 1, 2])
def test_exact_limit_and_bounded_read(monkeypatch, capsys, mode, extra):
    from datawrench.sqlmesh_manifest import validation

    assert validation.MAX_MANIFEST_BYTES == 64 * 1024 * 1024
    content = json.dumps(unchanged()).encode()
    limit = len(content) + 8
    content += b' ' * (limit - len(content) + extra)
    monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', limit)

    class Observed(io.BytesIO):
        def read(self, size=-1):
            assert size == limit + 1
            return super().read(size)

    stream = Observed(content)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    check(capsys, mode, extra == 0)
    assert stream.tell() == min(len(content), limit + 1)
    assert not stream.closed


@pytest.mark.parametrize('mode', MODES)
def test_dash_selection_and_literal_file(monkeypatch, capsys, tmp_path, mode):
    monkeypatch.chdir(tmp_path)
    literal = tmp_path / '-'
    original = json.dumps(unchanged()).encode()
    literal.write_bytes(original)
    stream = feed(monkeypatch, b'')
    check(capsys, mode, False)  # Never fall back to the valid literal file.
    check(capsys, mode, True, './-')
    check(capsys, mode, True, str(literal))
    assert stream.tell() == 0
    literal.write_bytes(b'{')
    stream = feed(monkeypatch, original)
    check(capsys, mode, True)  # stdin wins over the invalid literal file.
    check(capsys, mode, False, './-')
    assert literal.read_bytes() == b'{'
    assert not stream.closed


@pytest.mark.parametrize('args', [
    ['-', '--format', 'yaml'], ['-', '--format'], ['-', 'extra', '--format', 'json'],
    ['-', '--out', 'SECRET', '--format', 'json'], ['-', '--unknown'],
])
def test_argument_errors_do_not_read_stdin(monkeypatch, capsys, args):
    class Unreadable:
        @property
        def buffer(self):
            pytest.fail('Argument errors must not access stdin')

    monkeypatch.setattr(sys, 'stdin', Unreadable())
    with pytest.raises(SystemExit) as exc:
        main(['validate-diff', *args])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == '' and 'usage:' in err and 'error:' in err


@pytest.mark.parametrize('mode', MODES)
def test_stdin_offline_and_no_input_files(monkeypatch, capsys, mode):
    import requests
    import socket
    from datawrench.sqlmesh_manifest import cli, diff_validation, validation

    def blocked(*args, **kwargs):
        pytest.fail('Stdin validation must only read stdin and the packaged schema')

    for name in ('load_project', 'build_manifest', 'validate_manifest', 'read_manifest', '_write_atomic'):
        monkeypatch.setattr(cli, name, blocked)
    monkeypatch.setattr(diff_validation, 'read_json_artifact', blocked)
    monkeypatch.setattr(validation, 'parse_manifest', blocked)
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    data = changed('future_kind')
    data.update({'$schema': 'https://SECRET.invalid/schema', '$ref': 'https://SECRET.invalid/data'})
    feed(monkeypatch, json.dumps(data).encode())
    check(capsys, mode, True)
