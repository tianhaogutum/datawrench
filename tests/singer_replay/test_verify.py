"""Verification describes the validated source, with no replay/trim transforms."""
import copy
import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from datawrench.singer_replay.cassette import Cassette, scrub_rules, trim
from datawrench.singer_replay.cli import record_main
from datawrench.singer_replay.common import encode


def schema(stream):
    return {'type': 'SCHEMA', 'stream': stream, 'key_properties': ['id'],
            'replication_key': 'id', 'schema': {'type': 'object',
            'properties': {'id': {'type': 'integer'}}, 'required': ['id']}}


def rec(stream, n):
    return {'type': 'RECORD', 'stream': stream, 'record': {'id': n}}


def cli(source, *args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'singer-replay',
                           'verify', str(source), *map(str, args)],
                          capture_output=True, timeout=15)


def snapshot(root):
    result = {}
    for path in [root, *root.rglob('*')]:
        info = path.lstat()
        value = (path.read_bytes() if stat.S_ISREG(info.st_mode) else
                 os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, value)
    return result


def rewrite(source, *, messages=None, catalog=None, **changes):
    manifest = json.loads((source / 'manifest.json').read_bytes())
    if messages is not None:
        (source / 'messages.jsonl.gz').write_bytes(gzip.compress(
            ''.join(encode(m) + '\n' for m in messages).encode(), mtime=0))
    if catalog is not None:
        (source / 'catalog.json').write_text(encode(catalog) + '\n')
    for name in manifest['sha256']:
        manifest['sha256'][name] = hashlib.sha256((source / name).read_bytes()).hexdigest()
    manifest.update(changes)
    (source / 'manifest.json').write_text(encode(manifest) + '\n')


def test_verify_cli_actual_source_no_writes(tmp_path, make_cassette):
    a = schema('alpha')
    source = make_cassette([a, rec('alpha', 1), a, rec('alpha', 2), a])
    before = snapshot(tmp_path)
    expected = b'{"counts":{"alpha":2},"message_count":5,"truncated":false,"streams":["alpha"]}\n'
    for _ in range(2):
        result = cli(source)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected and result.stderr == b''
        assert snapshot(tmp_path) == before


@pytest.mark.parametrize('messages,counts,count', [
    ([schema('zero')], {'zero': 0}, 1),
    ([schema('zero'), schema('zero')], {'zero': 0}, 2),
    ([schema('zeta'), rec('zeta', 1), schema('zeta'), schema('alpha')],
     {'zeta': 1, 'alpha': 0}, 4),
    ([{'type': 'STATE', 'value': {'bookmarks': {}}}, schema('zeta'),
      schema('alpha'), rec('zeta', 1), rec('alpha', 1),
      {'type': 'STATE', 'value': {'bookmarks': {}}},
      {'type': 'STATE', 'value': {'bookmarks': {}}}], {'zeta': 1, 'alpha': 1}, 7),
])
def test_verify_source_message_count_and_stream_order(tmp_path, make_cassette, messages, counts, count):
    source = make_cassette(messages)
    before = snapshot(tmp_path)
    result = cli(source)
    assert result.returncode == 0 and result.stderr == b''
    assert result.stdout == (encode({'counts': counts, 'message_count': count,
                                     'truncated': False, 'streams': list(counts)}) + '\n').encode()
    assert snapshot(tmp_path) == before


def test_verify_inherited_truncation_scrub_and_trim_provenance(tmp_path, make_cassette):
    a = schema('alpha')
    a['schema']['properties']['private_field'] = {'type': 'string'}
    source = make_cassette([a, {'type': 'RECORD', 'stream': 'alpha',
                              'record': {'id': 1, 'private_field': 'synthetic-private'}}],
                           rules=scrub_rules(['alpha.private_field=mask']), limit=1)
    dest = tmp_path / 'trimmed'
    trim(source, dest, per_stream=2)
    before = snapshot(tmp_path)
    for path, count in [(source, 2), (dest, 3)]:
        result = cli(path)
        assert result.returncode == 0 and result.stderr == b''
        assert json.loads(result.stdout) == {'counts': {'alpha': 1}, 'message_count': count,
                                             'truncated': True, 'streams': ['alpha']}
        assert all(secret not in result.stdout for secret in
                   [b'private_field', b'synthetic-private', b'scrub', b'sha256', b'recorded_at'])
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('case', [
    'incomplete', 'counts', 'message_count', 'catalog', 'record', 'schema', 'state',
    'timestamp', 'json', 'not-object', 'no-schema', 'gzip-truncated', 'gzip-crc', 'gzip-tail',
    'manifest-json', 'manifest-type', 'catalog-json', 'checksum-messages', 'checksum-catalog',
])
def test_verify_whole_cassette_failure_empty_stdout(tmp_path, make_cassette, case):
    messages = [schema('alpha'), rec('alpha', 1), rec('alpha', 2)]
    source = make_cassette(messages)
    if case == 'incomplete':
        rewrite(source, complete=False)
    elif case == 'counts':
        rewrite(source, counts={'alpha': 1})
    elif case == 'message_count':
        rewrite(source, message_count=99)
    elif case == 'catalog':
        rewrite(source, catalog={'streams': []})
    elif case in ('record', 'schema', 'state', 'timestamp', 'not-object', 'no-schema'):
        if case == 'record':
            messages[-1]['record']['id'] = 'synthetic-private'
        elif case == 'schema':
            changed = copy.deepcopy(messages[0])
            changed['schema']['properties']['private_field'] = {'type': 'string'}
            messages.append(changed)
        elif case == 'state':
            messages.append({'type': 'STATE', 'value': {'private_field': 'synthetic-private'}})
        elif case == 'timestamp':
            messages[-1]['time_extracted'] = '2024-01-01T13:00:00+00:60'
        elif case == 'not-object':
            messages.append(['synthetic-private'])
        else:
            messages = [{'type': 'STATE', 'value': {'bookmarks': {}}}]
        rewrite(source, messages=messages)
    elif case == 'manifest-type':
        (source / 'manifest.json').write_text('[]')
    elif case == 'manifest-json':
        (source / 'manifest.json').write_text('{"private_field":"synthetic-private",')
    elif case in ('json', 'gzip-truncated', 'gzip-crc', 'gzip-tail'):
        path = source / 'messages.jsonl.gz'
        data = path.read_bytes()
        if case == 'json':
            data = gzip.compress(gzip.decompress(data) + b'{"private_field":"synthetic-private",\n')
        elif case == 'gzip-truncated':
            data = data[:-4]
        elif case == 'gzip-crc':
            data = data[:-8] + bytes([data[-8] ^ 1]) + data[-7:]
        else:
            data += b'synthetic-private'
        path.write_bytes(data)
        rewrite(source)
    elif case == 'catalog-json':
        (source / 'catalog.json').write_text('{"private_field":"synthetic-private",')
        rewrite(source)
    else:
        member = 'messages.jsonl.gz' if case == 'checksum-messages' else 'catalog.json'
        with (source / member).open('ab') as output:
            output.write(b'synthetic-private')
    before = snapshot(tmp_path)
    result = cli(source)
    assert result.returncode == 2 and result.stdout == b''
    assert result.stderr.startswith(b'datawrench singer-replay: ')
    assert b'synthetic-private' not in result.stderr and b'private_field' not in result.stderr
    assert b'Traceback' not in result.stderr
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('member', ['manifest.json', 'catalog.json', 'messages.jsonl.gz'])
@pytest.mark.parametrize('kind', ['missing', 'symlink', 'directory', 'fifo', 'oversize'])
def test_verify_rejects_bad_members(tmp_path, make_cassette, member, kind):
    source = make_cassette([schema('alpha')])
    path = source / member
    path.unlink()
    if kind == 'symlink':
        target = tmp_path / 'target'
        target.write_bytes(b'synthetic-private')
        path.symlink_to(target)
    elif kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'oversize':
        with path.open('wb') as output:
            output.truncate(1024 * 1024 * 1024 + 1)
    # Avoid reading the intentionally sparse >1-GiB member into memory.
    before = {p.name: (p.lstat().st_mode, p.lstat().st_size, p.lstat().st_mtime_ns)
              for p in source.iterdir()}
    result = cli(source)
    assert result.returncode == 2 and result.stdout == b''
    assert result.stderr and b'Traceback' not in result.stderr
    assert before == {p.name: (p.lstat().st_mode, p.lstat().st_size, p.lstat().st_mtime_ns)
                      for p in source.iterdir()}


@pytest.mark.parametrize('member', ['manifest.json', 'catalog.json'])
def test_verify_oversized_metadata(tmp_path, make_cassette, capsys, monkeypatch, member):
    source = make_cassette([schema('alpha')])
    path = source / member
    path.write_bytes(path.read_bytes() + b' ' * 2048)
    if member == 'catalog.json':
        rewrite(source)
    monkeypatch.setattr('datawrench.singer_replay.cassette.MAX_JSON', 2048)
    before = snapshot(tmp_path)
    assert record_main(['verify', str(source)]) == 2
    result = capsys.readouterr()
    assert result.out == '' and 'size limit' in result.err
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('limit', ['MAX_LINE', 'MAX_MESSAGES_BYTES'])
def test_verify_message_bounds(tmp_path, make_cassette, capsys, monkeypatch, limit):
    source = make_cassette([schema('alpha'), rec('alpha', 1)])
    monkeypatch.setattr('datawrench.singer_replay.common.' + limit, 100)
    before = snapshot(tmp_path)
    assert record_main(['verify', str(source)]) == 2
    result = capsys.readouterr()
    assert result.out == '' and 'exceed' in result.err
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_verify_summary_encoded_bound(tmp_path, make_cassette, capsys, monkeypatch, offset):
    source = make_cassette([schema('alpha')])
    expected = '{"counts":{"alpha":0},"message_count":1,"truncated":false,"streams":["alpha"]}\n'
    original = Cassette.__enter__
    def enter(cassette):
        result = original(cassette)
        monkeypatch.setattr('datawrench.singer_replay.cassette.MAX_JSON', len(expected.encode()) + offset)
        return result
    monkeypatch.setattr(Cassette, '__enter__', enter)
    before = snapshot(tmp_path)
    assert record_main(['verify', str(source)]) == (2 if offset < 0 else 0)
    result = capsys.readouterr()
    assert result.out == ('' if offset < 0 else expected)
    assert ('metadata' in result.err) if offset < 0 else result.err == ''
    assert snapshot(tmp_path) == before


def test_verify_uses_preflight_only(make_cassette, monkeypatch, capsys):
    from datawrench.singer_replay import cassette, cli as module
    source = make_cassette([schema('alpha'), rec('alpha', 1)])
    calls = []
    original = Cassette.messages
    def messages(self):
        calls.append(self)
        yield from original(self)
    def forbidden(*args, **kwargs):
        pytest.fail('verify invoked a transform or output writer')
    monkeypatch.setattr(Cassette, 'messages', messages)
    for name in ['trim', 'replay', 'record']:
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(cassette, '_trim_messages', forbidden)
    monkeypatch.setattr(cassette, '_record_messages', forbidden)
    assert record_main(['verify', str(source)]) == 0
    assert len(calls) == 1 and calls[0].raw.closed
    assert json.loads(capsys.readouterr().out)['message_count'] == 2


@pytest.mark.parametrize('args', [
    ['--per-stream', '0'], ['--schema-once'], ['--dry-run'], ['--config', 'unused'],
    ['--scrub', 'alpha.id=drop'], ['--output', 'unused'], ['extra-source'],
])
def test_verify_rejects_transform_and_extra_options(tmp_path, make_cassette, args):
    source = make_cassette([schema('alpha')])
    before = snapshot(tmp_path)
    result = cli(source, *args)
    assert result.returncode == 2 and result.stdout == b'' and result.stderr
    assert snapshot(tmp_path) == before


def test_verify_missing_source(tmp_path):
    result = cli(tmp_path / 'absent')
    assert result.returncode == 2 and result.stdout == b''
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'singer-replay', 'verify'],
                            capture_output=True, timeout=15)
    assert result.returncode == 2 and result.stdout == b''
