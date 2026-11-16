"""File publication must preserve inputs and an existing destination on failure."""
import errno
import json
from pathlib import Path
import resource
import signal
import subprocess
import sys

import pytest

import datawrench.tapcheck.cli as module
import datawrench.tapcheck.output as publication
from .conftest import SCHEMA, RECORD, STATE
from datawrench.tapcheck.io import dumps


def cli(*args, **kwargs):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', 'convert', *map(str, args)],
                          capture_output=True, **kwargs)


def write_capture(path, messages):
    raw = ('\n'.join(dumps(msg) for msg in messages) + '\n').encode()
    path.write_bytes(raw)
    return raw


@pytest.mark.parametrize('protocol,target', [('singer', 'airbyte'), ('airbyte', 'singer')])
@pytest.mark.parametrize('existing', [False, True])
def test_atomic_success_matches_stdout(tmp_path, protocol, target, existing):
    source = tmp_path / 'source.jsonl'
    raw = Path(f'examples/tapcheck/good_{protocol}.jsonl').read_bytes()
    source.write_bytes(raw)
    destination = tmp_path / 'output.jsonl'
    if existing:
        destination.write_bytes(b'old output\x00\xff')
    streamed = cli(source, '--to', target)
    published = cli(source, '--to', target, '--output', destination)
    assert streamed.returncode == published.returncode == 0, published.stderr
    assert published.stdout == published.stderr == b''
    assert destination.read_bytes() == streamed.stdout
    assert source.read_bytes() == raw
    assert set(tmp_path.iterdir()) == {source, destination}


@pytest.mark.parametrize('failure,code', [
    ('bad_record', 'TC006'), ('unsupported', 'TC023'),
    ('bad_json', 'TC001'), ('missing_state', 'TC008'),
])
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('protocol,target', [('singer', 'airbyte'), ('airbyte', 'singer')])
def test_conversion_failure_preserves_destination(tmp_path, failure, code, existing, protocol, target):
    source = tmp_path / 'source.jsonl'
    schema, record, state = SCHEMA, RECORD, STATE
    unsupported = {'type': 'ACTIVATE_VERSION', 'stream': 'users', 'version': 2}
    bad_record = dict(RECORD, record={'id': 'wrong'})
    if protocol == 'airbyte':
        schema, record, state = [json.loads(line) for line in Path('examples/tapcheck/good_airbyte.jsonl').read_bytes().splitlines()]
        unsupported = {'type': 'LOG', 'log': {}}
        bad_record = dict(record, record=dict(record['record'], data={'id': 'wrong'}))
    messages = [schema, record]
    if failure == 'bad_record':
        messages += [bad_record, state]
    elif failure == 'unsupported':
        messages += [unsupported, state]
    raw = write_capture(source, messages)
    if failure == 'bad_json':
        raw += b'{truncated\n'
        source.write_bytes(raw)
    destination = tmp_path / 'output.jsonl'
    old = b'previous complete output\x00\xff\n'
    if existing:
        destination.write_bytes(old)
    result = cli(source, '--to', target, '--output', destination)
    assert result.returncode == 2 and code.encode() in result.stderr
    assert result.stdout == b'' and b'Traceback' not in result.stderr
    assert destination.read_bytes() == old if existing else not destination.exists()
    assert source.read_bytes() == raw
    assert set(tmp_path.iterdir()) == ({source, destination} if existing else {source})


def test_filesystem_output_limit_preserves_destination(tmp_path):
    source = tmp_path / 'source.jsonl'
    raw = write_capture(source, [SCHEMA, dict(RECORD, record={'id': 1, 'note': 'x' * 20000}), STATE])
    destination = tmp_path / 'output.jsonl'
    old = b'existing destination\x00\xff'
    destination.write_bytes(old)

    def limit_output():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))

    result = cli(source, '--to', 'airbyte', '--output', destination, preexec_fn=limit_output)
    assert result.returncode == 2 and b'TC021' in result.stderr
    assert result.stdout == b'' and b'Traceback' not in result.stderr
    assert destination.read_bytes() == old and source.read_bytes() == raw
    assert set(tmp_path.iterdir()) == {source, destination}


def test_encoded_output_limit_after_first_message(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source.jsonl'
    raw = write_capture(source, [SCHEMA, dict(RECORD, record={'id': 1, 'note': 'x' * 2000}), STATE])
    destination = tmp_path / 'output.jsonl'
    destination.write_bytes(b'old')
    monkeypatch.setattr(module, 'MAX_LINE_BYTES', 1024)
    assert module.main(['convert', str(source), '--to', 'airbyte', '--output', str(destination)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'TC020' in captured.err
    assert destination.read_bytes() == b'old' and source.read_bytes() == raw
    assert set(tmp_path.iterdir()) == {source, destination}


@pytest.mark.parametrize('operation', ['fsync', 'replace'])
def test_finalization_failure_cleans_complete_temporary_file(tmp_path, monkeypatch, capsys, operation):
    source = tmp_path / 'source.jsonl'
    raw = write_capture(source, [SCHEMA, RECORD, STATE])
    destination = tmp_path / 'output.jsonl'
    destination.write_bytes(b'previous output')
    calls = []

    def fail(*args):
        temporary, = tmp_path.glob('.tapcheck-*.tmp')
        staged = [json.loads(line) for line in temporary.read_bytes().splitlines()]
        assert [msg['type'] for msg in staged] == ['CATALOG', 'RECORD', 'STATE']
        assert destination.read_bytes() == b'previous output'
        calls.append(operation)
        raise OSError(errno.ENOSPC, 'synthetic finalization failure')

    monkeypatch.setattr(publication.os, operation, fail)
    assert module.main(['convert', str(source), '--to', 'airbyte', '--output', str(destination)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'TC021' in captured.err
    assert calls == [operation]
    assert destination.read_bytes() == b'previous output' and source.read_bytes() == raw
    assert set(tmp_path.iterdir()) == {source, destination}


@pytest.mark.parametrize('alias', ['same', 'hardlink', 'symlink'])
def test_input_alias_is_rejected(tmp_path, alias):
    source = tmp_path / 'source.jsonl'
    raw = write_capture(source, [SCHEMA, RECORD, STATE])
    destination = source if alias == 'same' else tmp_path / 'output.jsonl'
    if alias == 'hardlink':
        destination.hardlink_to(source)
    elif alias == 'symlink':
        destination.symlink_to(source)
    result = cli(source, '--to', 'airbyte', '--output', destination)
    assert result.returncode == 2 and b'TC021' in result.stderr
    assert source.read_bytes() == destination.read_bytes() == raw
    assert set(tmp_path.iterdir()) == {source, destination}


def test_external_catalog_and_stdin_are_protected(tmp_path):
    catalog = tmp_path / 'catalog.json'
    raw_catalog = json.dumps({'streams': [SCHEMA]}).encode()
    catalog.write_bytes(raw_catalog)
    raw = ('\n'.join(dumps(msg) for msg in [RECORD, STATE]) + '\n').encode()
    destination = tmp_path / 'output.jsonl'
    streamed = cli('--to', 'airbyte', '--catalog', catalog, input=raw)
    result = cli('--to', 'airbyte', '--catalog', catalog, '--output', destination, input=raw)
    assert streamed.returncode == result.returncode == 0, result.stderr
    assert result.stdout == b'' and destination.read_bytes() == streamed.stdout
    refused = cli('--to', 'airbyte', '--catalog', catalog, '--output', catalog, input=raw)
    assert refused.returncode == 2 and b'TC021' in refused.stderr
    assert catalog.read_bytes() == raw_catalog
    # A redirected stdin file is still an input, even though its CLI name is '-'.
    with destination.open('rb') as handle:
        refused = cli('--to', 'singer', '--output', destination, stdin=handle)
    assert refused.returncode == 2 and b'TC021' in refused.stderr
    assert destination.read_bytes() == streamed.stdout
    assert set(tmp_path.iterdir()) == {catalog, destination}


@pytest.mark.parametrize('output_args', [(), ('--output', '-')])
def test_stdout_failure_warns_about_partial_output(output_args):
    raw = ('\n'.join(dumps(msg) for msg in [SCHEMA, RECORD]) + '\n').encode()
    result = cli('--to', 'airbyte', *output_args, input=raw)
    assert result.returncode == 2 and b'TC008' in result.stderr
    assert len(result.stdout.splitlines()) == 2
    assert b'stdout may contain partial output' in result.stderr
