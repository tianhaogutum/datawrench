"""Synthetic interleaved two-stream cassettes exercised through installed entrypoints."""
import copy
import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from datawrench.singer_replay.cassette import Cassette, scrub_rules, trim
from datawrench.singer_replay.common import ReplayError

# Former console scripts, now datawrench subcommands.
COMMANDS = {
    'singer-replay': [sys.executable, '-m', 'datawrench', 'singer-replay'],
    'tap-replay': [sys.executable, '-m', 'datawrench', 'singer-replay', 'tap'],
}


def cli(name, *args):
    return subprocess.run([*COMMANDS[name], *map(str, args)],
                          capture_output=True, timeout=15)


def replay_cli(tmp_path, path, *, state=None, **settings):
    config = tmp_path / 'replay-config.json'
    config.write_text(json.dumps({'cassette_path': str(path), **settings}))
    args = ['--config', config]
    if state is not None:
        state_path = tmp_path / 'resume.json'
        state_path.write_text(json.dumps(state))
        args += ['--state', state_path]
    result = cli('tap-replay', *args)
    return result, [json.loads(line) for line in result.stdout.splitlines()]


def rows(messages):
    return [m for m in messages if m['type'] == 'RECORD']


@pytest.fixture
def interleaved():
    schemas = [
        {'type': 'SCHEMA', 'stream': 'events', 'key_properties': ['id'],
         'replication_key': 'id', 'schema': {'type': 'object',
         'properties': {'id': {'type': 'integer'}}, 'required': ['id']}},
        {'type': 'SCHEMA', 'stream': 'audit.events', 'key_properties': ['id'],
         'bookmark_properties': ['at'], 'schema': {'type': 'object',
         'properties': {'id': {'type': 'integer'},
                        'at': {'type': 'string', 'format': 'date-time'}},
         'required': ['id', 'at']}}]
    messages = [{'type': 'STATE', 'value': {'bookmarks': {}}}, *schemas]
    bookmarks = {}
    for n in range(1, 4):
        for stream in ('events', 'audit.events'):
            record = {'id': n}
            key = 'id' if stream == 'events' else 'at'
            if key == 'at':
                record['at'] = f'2024-01-0{n}T00:00:00Z'
            messages.append({'type': 'RECORD', 'stream': stream, 'record': record})
            bookmarks[stream] = {'replication_key': key, 'replication_key_value': record[key]}
            messages.append({'type': 'STATE', 'value': {'bookmarks': copy.deepcopy(bookmarks)}})
    return messages


@pytest.mark.parametrize('limit', [0, 1, 2, 3, 4])
def test_trim_per_stream_boundaries_and_safe_states(tmp_path, make_cassette, interleaved, limit):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'subset'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', limit)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert cassette.catalog == json.loads(before['catalog.json'])
        assert cassette.manifest['counts'] == {'events': min(limit, 3), 'audit.events': min(limit, 3)}
        assert cassette.manifest['truncated'] is (limit < 3)
        # Independent prefix oracle: every saved bookmark is exactly the latest emitted key.
        latest = {}
        for message in output:
            if message['type'] == 'RECORD':
                stream, record = message['stream'], message['record']
                key = 'id' if stream == 'events' else 'at'
                latest[stream] = {'replication_key': key, 'replication_key_value': record[key]}
            elif message['type'] == 'STATE':
                assert message['value'] == {'bookmarks': latest}
        assert output[-1] == {'type': 'STATE', 'value': {'bookmarks': latest}}
        assert rows(output) == [m for m in rows(interleaved) if m['record']['id'] <= limit]
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert os.stat(dest).st_mode & 0o777 == 0o700
    assert all(os.stat(p).st_mode & 0o777 == 0o600 for p in dest.iterdir())
    result, replayed = replay_cli(tmp_path, dest)
    assert result.returncode == 0, result.stderr
    assert rows(replayed) == rows(output)
    result = cli('tap-replay', '--config', tmp_path / 'replay-config.json', '--discover')
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)['streams']) == 2


@pytest.mark.parametrize('fail_after', [0, 1, 2, 3, 4])
def test_trim_two_keyed_streams_failure_resume_exactly_once(tmp_path, make_cassette, interleaved, fail_after):
    source = make_cassette(interleaved)
    dest = tmp_path / 'subset'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2)
    assert result.returncode == 0, result.stderr
    result, before = replay_cli(tmp_path, dest, fail_after_records=fail_after, fail_exit_code=19)
    assert result.returncode == 19, result.stderr
    assert len(rows(before)) == fail_after and before[-1]['type'] == 'STATE'
    result, after = replay_cli(tmp_path, dest, state=before[-1]['value'])
    assert result.returncode == 0, result.stderr
    expected = [m for m in rows(interleaved) if m['record']['id'] <= 2]
    assert rows(before) + rows(after) == expected
    result, final = replay_cli(tmp_path, dest, state=after[-1]['value'])
    assert result.returncode == 0 and rows(final) == []


def test_trim_deterministic_scrubbed_shop_and_full_table(tmp_path, make_cassette, messages):
    source = make_cassette(messages, rules=scrub_rules(['customers.email=hash']), salt=b'T' * 32)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    destinations = [tmp_path / 'a', tmp_path / 'b']
    for dest in destinations:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1)
        assert result.returncode == 0, result.stderr
    assert {p.name: p.read_bytes() for p in destinations[0].iterdir()} == {
        p.name: p.read_bytes() for p in destinations[1].iterdir()}
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    source_manifest = json.loads(before['manifest.json'])
    with Cassette(destinations[0]) as cassette:
        assert cassette.manifest['scrub'] == source_manifest['scrub']
        assert cassette.manifest['recorded_at'] == source_manifest['recorded_at']
        assert cassette.manifest['trim'] == {
            'per_stream': 1, 'source_sha256': source_manifest['sha256']}
        output = list(cassette.messages())
    assert [m['record'].get('id', m['record'].get('sku')) for m in rows(output)] == [1, 'DEMO-1']
    assert output[-1]['value'] == messages[4]['value']
    assert b'alice@example.invalid' not in gzip.decompress((destinations[0] / 'messages.jsonl.gz').read_bytes())
    result, resumed = replay_cli(tmp_path, destinations[0], state=output[-1]['value'])
    assert result.returncode == 0, result.stderr
    assert [m['stream'] for m in rows(resumed)] == ['products']  # Full-table contract retained.


@pytest.mark.parametrize('limit', ['-1', '1.5', 'abc'])
def test_trim_invalid_limits_do_not_create_destination(tmp_path, make_cassette, messages, limit):
    source = make_cassette(messages)
    dest = tmp_path / 'bad-limit'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', limit)
    assert result.returncode == 2 and result.stdout == b''
    assert not dest.exists()


@pytest.mark.parametrize('member', ['messages.jsonl.gz', 'catalog.json'])
def test_trim_validates_entire_source_even_for_zero_subset(tmp_path, make_cassette, messages, member):
    source = make_cassette(messages)
    with (source / member).open('ab') as file:
        file.write(b'corruption')
    dest = tmp_path / 'corrupt-subset'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0)
    assert result.returncode == 2 and result.stdout == b''
    assert b'checksum' in result.stderr and not dest.exists()


def test_trim_no_overwrite_and_no_source_alias(tmp_path, make_cassette, messages):
    source = make_cassette(messages)
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'sentinel').write_bytes(b'keep')
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    for dest in [source, alias, existing]:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1)
        assert result.returncode == 2 and result.stdout == b''
        assert b'overwrite' in result.stderr
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert (existing / 'sentinel').read_bytes() == b'keep'
    result = cli('singer-replay', 'trim', source, '-o', source / 'child', '--per-stream', 1)
    assert result.returncode == 2 and b'outside the source' in result.stderr
    assert not (source / 'child').exists()


def test_trim_preflights_invalid_discarded_record(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    interleaved[-2]['record']['id'] = 'invalid'
    data = gzip.compress(('\n'.join(json.dumps(m) for m in interleaved) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
    (source / 'manifest.json').write_text(json.dumps(manifest))
    dest = tmp_path / 'bad-subset'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1)
    assert result.returncode == 2 and result.stdout == b''
    assert b'schema type' in result.stderr and not dest.exists()


@pytest.mark.parametrize('checkpoints', [{}, {'final_state_only': True}, {'state_every': 2}, {'state_every': 3}])
@pytest.mark.parametrize('bound', ['MAX_LINE', 'MAX_MESSAGES_BYTES'])
def test_trim_expanded_states_respect_output_size_limits(tmp_path, make_cassette,
                                                        monkeypatch, bound, checkpoints):
    # Empty source checkpoints are valid but expand to both retained stream bookmarks.
    messages = []
    for stream in ['left', 'right']:
        messages.extend([
            {'type': 'SCHEMA', 'stream': stream, 'key_properties': [], 'replication_key': 'id',
             'schema': {'type': 'object', 'properties': {'id': {'type': 'string'}}}},
            {'type': 'RECORD', 'stream': stream, 'record': {'id': 'x' * 1000}},
            {'type': 'STATE', 'value': {'bookmarks': {}}}])
    source = make_cassette(messages)
    source_sizes = [len(json.dumps(m, separators=(',', ':')).encode()) + 1 for m in messages]
    # Simulate the format's byte ceilings with a small fixture, leaving input limits intact.
    monkeypatch.setattr('datawrench.singer_replay.cassette.' + bound,
                        max(source_sizes) if bound == 'MAX_LINE' else sum(source_sizes))
    dest = tmp_path / 'too-large'
    with pytest.raises(ReplayError, match='output message'):
        trim(source, dest, per_stream=3, **checkpoints)
    assert json.loads((dest / 'manifest.json').read_text())['complete'] is False
    with pytest.raises(ReplayError, match='incomplete'):
        with Cassette(dest):
            pass


def test_trim_adds_final_state_without_source_boundaries(tmp_path, make_cassette, interleaved):
    source = make_cassette([m for m in interleaved if m['type'] != 'STATE'], limit=5)
    dest = tmp_path / 'subset'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 10)
    assert result.returncode == 0, result.stderr
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert cassette.manifest['truncated'] is True  # Retains source truncation provenance.
        assert output[-1] == {'type': 'STATE', 'value': {'bookmarks': {
            'events': {'replication_key': 'id', 'replication_key_value': 3},
            'audit.events': {'replication_key': 'at', 'replication_key_value': '2024-01-02T00:00:00Z'}}}}


@pytest.mark.parametrize('default,overrides,caps', [
    (2, ['events=0', 'audit.events=1'], {'events': 0, 'audit.events': 1}),
    (2, ['events=1'], {'events': 1, 'audit.events': 2}),
    (0, ['events=2', 'audit.events=1'], {'events': 2, 'audit.events': 1}),
    (3, ['events=0', 'audit.events=0'], {'events': 0, 'audit.events': 0}),
    (0, ['events=4', 'audit.events=3'], {'events': 4, 'audit.events': 3}),
])
def test_trim_stream_overrides_contract(tmp_path, make_cassette, interleaved,
                                        default, overrides, caps):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    options = [arg for value in overrides for arg in ('--stream-limit', value)]
    dest = tmp_path / 'subset'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', default, *options)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    expected = [m for m in rows(interleaved) if m['record']['id'] <= caps[m['stream']]]
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert rows(output) == expected
        assert cassette.manifest['counts'] == {s: min(n, 3) for s, n in caps.items()}
        assert cassette.manifest['truncated'] is any(n < 3 for n in caps.values())
        assert cassette.catalog == json.loads(before['catalog.json'])
        assert cassette.manifest['trim'] == {
            'per_stream': default, 'stream_limits': {
                value.split('=')[0]: int(value.split('=')[1]) for value in overrides},
            'source_sha256': json.loads(before['manifest.json'])['sha256']}
        latest = {}
        for message in output:
            if message['type'] == 'RECORD':
                stream, record = message['stream'], message['record']
                key = 'id' if stream == 'events' else 'at'
                latest[stream] = {'replication_key': key, 'replication_key_value': record[key]}
            elif message['type'] == 'STATE':
                assert message['value'] == {'bookmarks': latest}
        assert sum(m['type'] == 'STATE' for m in output) == sum(
            m['type'] == 'STATE' for m in interleaved)
        assert output[-1]['value'] == {'bookmarks': latest}
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    result, replayed = replay_cli(tmp_path, dest)
    assert result.returncode == 0, result.stderr
    assert rows(replayed) == expected
    # Every possible failure checkpoint resumes without skipping/duplicating keyed records.
    for cut in range(len(expected) + 1):
        result, prefix = replay_cli(tmp_path, dest, fail_after_records=cut, fail_exit_code=19,
                                    state_message_frequency=1)
        assert result.returncode == 19, result.stderr
        assert len(rows(prefix)) == cut and prefix[-1]['type'] == 'STATE'
        result, suffix = replay_cli(tmp_path, dest, state=prefix[-1]['value'])
        assert result.returncode == 0, result.stderr
        assert rows(prefix) + rows(suffix) == expected
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('overrides,error', [
    (['events'], b'stream-limit must be STREAM=N'),
    (['=1'], b'stream-limit must be STREAM=N'),
    (['events='], b'stream-limit must be STREAM=N'),
    (['events=1=2'], b'stream-limit must be STREAM=N'),
    (['events=-1'], b'stream-limit must be STREAM=N'),
    (['events=1.5'], b'stream-limit must be STREAM=N'),
    (['events=abc'], b'stream-limit must be STREAM=N'),
    (['events=+1'], b'stream-limit must be STREAM=N'),
    (['events= 1'], b'stream-limit must be STREAM=N'),
    (['events=1 '], b'stream-limit must be STREAM=N'),
    (['events=١'], b'stream-limit must be STREAM=N'),
    (['events=1e2'], b'stream-limit must be STREAM=N'),
    (['events/child=1'], b'stream-limit must be STREAM=N'),
    (['events\n=1'], b'stream-limit must be STREAM=N'),
    (['absent=1'], b'stream-limit names an unknown stream'),
    (['Events=1'], b'stream-limit names an unknown stream'),
    (['events=1', 'events=1'], b'duplicate stream-limit'),
    (['events=0', 'audit.events=2', 'events=1'], b'duplicate stream-limit'),
])
def test_trim_stream_overrides_reject_before_output(tmp_path, make_cassette, interleaved,
                                                   overrides, error):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'bad-subset'
    options = [arg for value in overrides for arg in ('--stream-limit', value)]
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2, *options)
    assert result.returncode == 2 and result.stdout == b''
    assert error in result.stderr
    assert not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_stream_overrides_deterministic_order_and_schema_only(tmp_path, make_cassette,
                                                                  interleaved):
    # IDs come from schemas, including streams with no RECORDs.
    source = make_cassette([m for m in interleaved if m['type'] == 'SCHEMA' or
                            (m['type'] == 'RECORD' and m['stream'] == 'events')])
    outputs = []
    for n, values in enumerate([['events=01', 'audit.events=0'],
                                ['audit.events=00', 'events=1']]):
        dest = tmp_path / f'subset-{n}'
        options = [arg for value in values for arg in ('--stream-limit', value)]
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2, *options)
        assert result.returncode == 0, result.stderr
        with Cassette(dest) as cassette:
            assert cassette.manifest['counts'] == {'events': 1, 'audit.events': 0}
            assert len(cassette.catalog['streams']) == 2
            assert list(cassette.messages())[-1]['value'] == {'bookmarks': {
                'events': {'replication_key': 'id', 'replication_key_value': 1}}}
        outputs.append({p.name: p.read_bytes() for p in dest.iterdir()})
    assert outputs[0] == outputs[1]


def test_trim_stream_overrides_preflight_all_discarded_records(tmp_path, make_cassette,
                                                             interleaved):
    source = make_cassette(interleaved)
    interleaved[-2]['record']['id'] = 'invalid'
    data = gzip.compress(('\n'.join(json.dumps(m) for m in interleaved) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
    (source / 'manifest.json').write_text(json.dumps(manifest))
    dest = tmp_path / 'bad-subset'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 3,
                 '--stream-limit', 'events=0', '--stream-limit', 'audit.events=0')
    assert result.returncode == 2 and result.stdout == b''
    assert b'schema type' in result.stderr and not dest.exists()


@pytest.mark.parametrize('included,default,overrides,caps', [
    (['audit.events'], 1, [], {'audit.events': 1}),
    (['events'], 0, [], {'events': 0}),
    (['audit.events', 'events'], 2, ['events=1'], {'events': 1, 'audit.events': 2}),
    (['events', 'audit.events'], 0, ['audit.events=2'], {'events': 0, 'audit.events': 2}),
    (['audit.events'], 2, ['audit.events=4'], {'audit.events': 4}),
])
def test_trim_include_stream_contract(tmp_path, make_cassette, interleaved,
                                      included, default, overrides, caps):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    options = [arg for s in included for arg in ('--include-stream', s)]
    options += [arg for value in overrides for arg in ('--stream-limit', value)]
    dest = tmp_path / 'included'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', default, *options)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    expected = [m for m in rows(interleaved)
                if m['stream'] in caps and m['record']['id'] <= caps[m['stream']]]
    schemas = [m for m in interleaved if m['type'] == 'SCHEMA' and m['stream'] in caps]
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert rows(output) == expected
        assert [m for m in output if m['type'] == 'SCHEMA'] == schemas
        assert cassette.catalog == {'streams': [entry for entry in json.loads(before['catalog.json'])['streams']
                                                if entry['tap_stream_id'] in caps]}
        assert cassette.manifest['counts'] == {s: min(n, 3) for s, n in caps.items()}
        assert cassette.manifest['message_count'] == len(output)
        assert cassette.manifest['truncated'] is (len(expected) < 6)
        assert cassette.manifest['trim'] == {
            'per_stream': default, 'include_streams': sorted(included),
            **({'stream_limits': {s: int(n) for s, n in (v.split('=') for v in overrides)}}
               if overrides else {}),
            'source_sha256': json.loads(before['manifest.json'])['sha256']}
        latest = {}
        for message in output:
            if message['type'] == 'RECORD':
                stream, record = message['stream'], message['record']
                key = 'id' if stream == 'events' else 'at'
                latest[stream] = {'replication_key': key, 'replication_key_value': record[key]}
            elif message['type'] == 'STATE':
                assert message['value'] == {'bookmarks': latest}
        assert output[-1]['value'] == {'bookmarks': latest}
        assert sum(m['type'] == 'STATE' for m in output) == sum(
            m['type'] == 'STATE' for m in interleaved)
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    result, replayed = replay_cli(tmp_path, dest)
    assert result.returncode == 0, result.stderr
    assert rows(replayed) == expected
    discovery = cli('tap-replay', '--config', tmp_path / 'replay-config.json', '--discover')
    assert discovery.returncode == 0, discovery.stderr
    assert json.loads(discovery.stdout) == cassette.catalog
    for cut in range(len(expected) + 1):
        result, prefix = replay_cli(tmp_path, dest, fail_after_records=cut, fail_exit_code=19)
        assert result.returncode == 19, result.stderr
        assert len(rows(prefix)) == cut and prefix[-1]['type'] == 'STATE'
        result, suffix = replay_cli(tmp_path, dest, state=prefix[-1]['value'])
        assert result.returncode == 0, result.stderr
        assert rows(prefix) + rows(suffix) == expected
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('included,overrides,error', [
    ([''], [], b'include-stream'),
    (['absent'], [], b'include-stream names an unknown stream'),
    (['Events'], [], b'include-stream names an unknown stream'),
    (['events/child'], [], b'include-stream names an unknown stream'),
    (['events', 'events'], [], b'duplicate include-stream'),
    (['audit.events', 'events', 'audit.events'], [], b'duplicate include-stream'),
    (['audit.events'], ['events=0'], b'stream-limit names an excluded stream'),
    (['audit.events'], ['absent=0'], b'stream-limit names an unknown stream'),
])
def test_trim_include_stream_reject_before_output(tmp_path, make_cassette, interleaved,
                                                 included, overrides, error):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'bad'
    options = [arg for s in included for arg in ('--include-stream', s)]
    options += [arg for value in overrides for arg in ('--stream-limit', value)]
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1, *options)
    assert result.returncode == 2 and result.stdout == b''
    assert error in result.stderr
    assert not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_include_stream_deterministic_and_schema_only(tmp_path, make_cassette, interleaved):
    # No source STATEs: final checkpoint must be added, including for a schema-only selection.
    source = make_cassette([m for m in interleaved if m['type'] == 'SCHEMA' or
                            (m['type'] == 'RECORD' and m['stream'] == 'events')])
    outputs = []
    for i, included in enumerate([['events', 'audit.events'], ['audit.events', 'events']]):
        dest = tmp_path / f'ordered-{i}'
        options = [arg for s in included for arg in ('--include-stream', s)]
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2,
                     '--stream-limit', 'audit.events=0', *options)
        assert result.returncode == 0, result.stderr
        with Cassette(dest) as cassette:
            assert cassette.manifest['counts'] == {'events': 2, 'audit.events': 0}
            assert list(cassette.messages())[-1]['value'] == {'bookmarks': {
                'events': {'replication_key': 'id', 'replication_key_value': 2}}}
        outputs.append({p.name: p.read_bytes() for p in dest.iterdir()})
    assert outputs[0] == outputs[1]
    dest = tmp_path / 'schema-only'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 3,
                 '--include-stream', 'audit.events')
    assert result.returncode == 0, result.stderr
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert cassette.manifest['counts'] == {'audit.events': 0}
        assert cassette.manifest['truncated'] is True  # All events records were excluded.
        assert output == [interleaved[2], {'type': 'STATE', 'value': {'bookmarks': {}}}]
    # Removing only a schema doesn't truncate any records.
    next_dest = tmp_path / 'remove-empty-schema'
    result = cli('singer-replay', 'trim', source, '-o', next_dest, '--per-stream', 3,
                 '--include-stream', 'events')
    assert result.returncode == 0, result.stderr
    with Cassette(next_dest) as cassette:
        assert cassette.manifest['truncated'] is False


@pytest.mark.parametrize('limit', [0, 3])
def test_trim_include_stream_preflights_excluded_records(tmp_path, make_cassette, interleaved, limit):
    source = make_cassette(interleaved)
    interleaved[-2]['record']['id'] = 'invalid'  # Excluded audit.events record.
    data = gzip.compress(('\n'.join(json.dumps(m) for m in interleaved) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
    (source / 'manifest.json').write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'bad'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', limit,
                 '--include-stream', 'events')
    assert result.returncode == 2 and result.stdout == b''
    assert b'schema type' in result.stderr and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_include_stream_destination_guards(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'sentinel').write_bytes(b'keep')
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    for dest in [source, alias, existing, source / 'child', alias / 'child']:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1,
                     '--include-stream', 'events')
        assert result.returncode == 2 and result.stdout == b''
    assert not (source / 'child').exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert (existing / 'sentinel').read_bytes() == b'keep'


@pytest.mark.parametrize('included', [[], 'events', [None], ['events', 'events']])
def test_trim_include_stream_api_rejects_invalid_selection(tmp_path, make_cassette,
                                                         interleaved, included):
    source = make_cassette(interleaved)
    dest = tmp_path / 'bad'
    with pytest.raises(ReplayError, match='include-stream'):
        trim(source, dest, per_stream=1, include_streams=included)
    assert not dest.exists()


def test_trim_compact_states_contract(tmp_path, make_cassette):
    schema = {'type': 'SCHEMA', 'stream': 'beta', 'key_properties': ['id'],
              'replication_key': 'id', 'schema': {'type': 'object',
              'properties': {'id': {'type': 'integer'}}}}
    record1 = {'type': 'RECORD', 'stream': 'beta', 'record': {'id': 1}}
    record2 = {**record1, 'record': {'id': 2}}
    state1 = {'type': 'STATE', 'value': {'bookmarks': {
        'beta': {'replication_key': 'id', 'replication_key_value': 1}}}}
    state2 = {'type': 'STATE', 'value': {'bookmarks': {
        'beta': {'replication_key': 'id', 'replication_key_value': 2}}}}
    source = make_cassette([schema, record1, state1, record2, state2, state2])
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'compact'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1,
                 '--include-stream', 'beta', '--compact-states')
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == [schema, record1, state1]
        assert cassette.manifest['counts'] == {'beta': 1}
        assert cassette.manifest['message_count'] == 3
        assert cassette.manifest['trim']['compact_states'] is True
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('included,default,overrides,caps,states', [
    (['audit.events'], 1, [], {'audit.events': 1}, 3),
    (['events', 'audit.events'], 2, [], {'events': 2, 'audit.events': 2}, 5),
    (['audit.events', 'events'], 2, ['events=1'], {'events': 1, 'audit.events': 2}, 4),
    (['events', 'audit.events'], 0, [], {'events': 0, 'audit.events': 0}, 2),
    (['audit.events', 'events'], 3, [], {'events': 3, 'audit.events': 3}, 7),
])
def test_trim_compact_states_interleaved_resume(tmp_path, make_cassette, interleaved,
                                                included, default, overrides, caps, states):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    options = [arg for s in included for arg in ('--include-stream', s)]
    options += [arg for value in overrides for arg in ('--stream-limit', value)]
    dest = tmp_path / 'compact'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', default,
                 '--compact-states', *options)
    assert result.returncode == 0, result.stderr
    expected = [m for m in rows(interleaved)
                if m['stream'] in caps and m['record']['id'] <= caps[m['stream']]]
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert rows(output) == expected
        assert sum(m['type'] == 'STATE' for m in output) == states
        assert cassette.manifest['counts'] == {s: min(n, 3) for s, n in caps.items()}
        assert cassette.manifest['message_count'] == len(output)
        assert cassette.manifest['truncated'] is (len(expected) < 6)
        assert cassette.manifest['trim'] == {
            'per_stream': default, 'include_streams': sorted(included), 'compact_states': True,
            **({'stream_limits': {'events': 1}} if overrides else {}),
            'source_sha256': json.loads(before['manifest.json'])['sha256']}
        latest = {}
        for m in output:
            if m['type'] == 'RECORD':
                s, record = m['stream'], m['record']
                key = 'id' if s == 'events' else 'at'
                latest[s] = {'replication_key': key, 'replication_key_value': record[key]}
            elif m['type'] == 'STATE':
                assert m['value'] == {'bookmarks': latest}
        assert output[-1] == {'type': 'STATE', 'value': {'bookmarks': latest}}
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    for cut in range(len(expected) + 1):
        shifts = ('2030-01-01T00:00:00Z', '2040-01-01T00:00:00Z') if expected else (False, False)
        result, prefix = replay_cli(tmp_path, dest, fail_after_records=cut, fail_exit_code=19,
                                    time_shift=shifts[0])
        assert result.returncode == 19, result.stderr
        assert len(rows(prefix)) == cut and prefix[-1]['type'] == 'STATE'
        result, suffix = replay_cli(tmp_path, dest, state=prefix[-1]['value'], time_shift=shifts[1])
        assert result.returncode == 0, result.stderr
        assert [(m['stream'], m['record']['id']) for m in rows(prefix) + rows(suffix)] == [
            (m['stream'], m['record']['id']) for m in expected]
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_compact_states_adjacency_after_filtering(tmp_path, make_cassette):
    # Full-table records and repeated included schemas must break STATE adjacency.
    schema = {'type': 'SCHEMA', 'stream': 'beta', 'key_properties': ['id'],
              'schema': {'type': 'object', 'properties': {'id': {'type': 'integer'}}}}
    record1 = {'type': 'RECORD', 'stream': 'beta', 'record': {'id': 1}}
    empty = {'type': 'STATE', 'value': {'bookmarks': {}}}
    excluded = {**schema, 'stream': 'alpha', 'replication_key': 'id'}
    source = make_cassette([
        schema, empty, empty, record1, empty, empty, schema, empty, empty, excluded,
        {'type': 'RECORD', 'stream': 'alpha', 'record': {'id': 1}},
        {'type': 'STATE', 'value': {'bookmarks': {
            'alpha': {'replication_key': 'id', 'replication_key_value': 1}}}}])
    dest = tmp_path / 'adjacency'
    trim(source, dest, per_stream=1, include_streams=['beta'], compact_states=True)
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == [schema, empty, record1, empty, schema, empty]
        assert cassette.manifest['counts'] == {'beta': 1}
    result, resumed = replay_cli(tmp_path, dest, state={'bookmarks': {}})
    assert result.returncode == 0 and rows(resumed) == [record1]


@pytest.mark.parametrize('with_boundaries', [False, True])
def test_trim_compact_states_final_and_deterministic(tmp_path, make_cassette, interleaved,
                                                    with_boundaries):
    messages = interleaved if with_boundaries else [m for m in interleaved if m['type'] != 'STATE']
    source = make_cassette(messages)
    outputs = []
    for i, included in enumerate([['events', 'audit.events'], ['audit.events', 'events']]):
        dest = tmp_path / f'compact-{i}'
        trim(source, dest, per_stream=0, include_streams=included, compact_states=True)
        with Cassette(dest) as cassette:
            output = list(cassette.messages())
            assert rows(output) == []
            assert output[-1] == {'type': 'STATE', 'value': {'bookmarks': {}}}
            assert sum(m['type'] == 'STATE' for m in output) == (2 if with_boundaries else 1)
        outputs.append({p.name: p.read_bytes() for p in dest.iterdir()})
    assert outputs[0] == outputs[1]
    # Explicit false and omitted preserve the old artifacts, including provenance.
    legacy = tmp_path / 'legacy'
    explicit = tmp_path / 'explicit-false'
    trim(source, legacy, per_stream=0)
    trim(source, explicit, per_stream=0, compact_states=False)
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == {
        p.name: p.read_bytes() for p in explicit.iterdir()}
    assert 'compact_states' not in json.loads((legacy / 'manifest.json').read_text())['trim']


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_trim_compact_states_requires_boolean(tmp_path, make_cassette, interleaved, value):
    source = make_cassette(interleaved)
    dest = tmp_path / 'invalid'
    with pytest.raises(ReplayError, match='compact-states must be boolean'):
        trim(source, dest, per_stream=1, compact_states=value)
    assert not dest.exists()


@pytest.mark.parametrize('options', [
    ['--compact-states=true'], ['--compact-states', 'true'],
    ['--compact-states', '--include-stream', 'absent'],
    ['--compact-states', '--include-stream', 'events', '--include-stream', 'events'],
    ['--compact-states', '--include-stream', ''],
    ['--compact-states', '--include-stream', 'events', '--stream-limit', 'audit.events=0'],
])
def test_trim_compact_states_invalid_cli(tmp_path, make_cassette, interleaved, options):
    source = make_cassette(interleaved)
    dest = tmp_path / 'invalid'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1, *options)
    assert result.returncode == 2 and result.stdout == b''
    assert not dest.exists()


@pytest.mark.parametrize('corruption', ['excluded-record', 'discarded-state'])
def test_trim_compact_states_preflights_all_messages(tmp_path, make_cassette, interleaved, corruption):
    source = make_cassette(interleaved)
    if corruption == 'excluded-record':
        interleaved[-2]['record']['id'] = 'invalid'
    else:
        # This source state would become an empty duplicate after zero-capping.
        interleaved[-1]['value']['bookmarks']['audit.events']['replication_key_value'] = 'invalid'
    data = gzip.compress(('\n'.join(json.dumps(m) for m in interleaved) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
    (source / 'manifest.json').write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'bad'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0,
                 '--include-stream', 'events', '--compact-states')
    assert result.returncode == 2 and result.stdout == b''
    assert not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_compact_states_destination_guards(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    for dest in [source, alias, source / 'child', alias / 'child']:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1,
                     '--compact-states')
        assert result.returncode == 2 and result.stdout == b''
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('options,error', [
    (['--exclude-stream', ''], b'exclude-stream'),
    (['--exclude-stream', 'absent'], b'exclude-stream names an unknown stream'),
    (['--exclude-stream', 'Events'], b'exclude-stream names an unknown stream'),
    (['--exclude-stream', 'events/child'], b'exclude-stream names an unknown stream'),
    (['--exclude-stream', 'events', '--exclude-stream', 'events'], b'duplicate exclude-stream'),
    (['--exclude-stream', 'events', '--exclude-stream', 'audit.events'], b'exclude every stream'),
    (['--exclude-stream', 'events', '--include-stream', 'audit.events'], b'not allowed with argument'),
    (['--exclude-stream', 'events', '--include-stream', 'events'], b'not allowed with argument'),
    (['--exclude-stream', 'events', '--stream-limit', 'events=0'], b'stream-limit names an excluded stream'),
    (['--exclude-stream', 'events', '--stream-limit', 'absent=0'], b'stream-limit names an unknown stream'),
])
def test_trim_exclude_stream_reject_before_output(tmp_path, make_cassette, interleaved,
                                                  options, error):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'invalid'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1, *options)
    assert result.returncode == 2 and result.stdout == b''
    assert error in result.stderr and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('excluded', [[], 'events', ('events',), {'events'}, {}, [None],
                                     [1], [['events']], [''], ['events', 'events']])
def test_trim_exclude_stream_api_invalid(tmp_path, make_cassette, interleaved, excluded):
    source = make_cassette(interleaved)
    dest = tmp_path / 'invalid'
    with pytest.raises(ReplayError, match='exclude-stream'):
        trim(source, dest, per_stream=1, exclude_streams=excluded)
    assert not dest.exists()


def test_trim_exclude_stream_api_conflict(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    dest = tmp_path / 'invalid'
    with pytest.raises(ReplayError, match='include-stream and exclude-stream'):
        trim(source, dest, per_stream=1, include_streams=['events'], exclude_streams=['audit.events'])
    assert not dest.exists()


@pytest.mark.parametrize('compact', [False, True])
def test_trim_exclude_stream_deterministic_and_empty_schema(tmp_path, make_cassette,
                                                            interleaved, compact):
    empty = {**interleaved[1], 'stream': 'empty'}
    # No source STATEs: the final safe state must be added.
    source = make_cassette([*filter(lambda m: m['type'] != 'STATE', interleaved), empty])
    outputs = []
    for i, excluded in enumerate([['events', 'empty'], ['empty', 'events']]):
        dest = tmp_path / f'ordered-{i}'
        trim(source, dest, per_stream=0, stream_limits={'audit.events': 2},
             exclude_streams=excluded, compact_states=compact)
        with Cassette(dest) as cassette:
            assert cassette.manifest['trim']['exclude_streams'] == ['empty', 'events']
            assert list(cassette.messages())[-1]['value'] == {'bookmarks': {
                'audit.events': {'replication_key': 'at', 'replication_key_value': '2024-01-02T00:00:00Z'}}}
        outputs.append({p.name: p.read_bytes() for p in dest.iterdir()})
    assert outputs[0] == outputs[1]
    # Removing only an empty schema does not truncate records.
    dest = tmp_path / 'remove-empty'
    trim(source, dest, per_stream=3, exclude_streams=['empty'])
    with Cassette(dest) as cassette:
        assert cassette.manifest['truncated'] is False
    # Explicit None preserves default bytes/provenance.
    for name, kwargs in [('default', {}), ('explicit-none', {'exclude_streams': None})]:
        trim(source, tmp_path / name, per_stream=1, **kwargs)
    assert {p.name: p.read_bytes() for p in (tmp_path / 'default').iterdir()} == {
        p.name: p.read_bytes() for p in (tmp_path / 'explicit-none').iterdir()}
    assert 'exclude_streams' not in json.loads((tmp_path / 'default/manifest.json').read_text())['trim']
    dest = tmp_path / 'only-empty'
    trim(source, dest, per_stream=3, exclude_streams=['events', 'audit.events'], compact_states=compact)
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == [empty, {'type': 'STATE', 'value': {'bookmarks': {}}}]
        assert cassette.manifest['counts'] == {'empty': 0}
        assert cassette.manifest['truncated'] is True


def test_trim_exclude_stream_full_table_and_scrub_provenance(tmp_path, make_cassette, messages):
    source = make_cassette(messages, rules=scrub_rules(['customers.email=hash']), salt=b'T' * 32,
                           limit=3)
    source_manifest = json.loads((source / 'manifest.json').read_text())
    dest = tmp_path / 'products'
    trim(source, dest, per_stream=1, exclude_streams=['customers'], compact_states=True)
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert cassette.manifest['scrub'] == source_manifest['scrub']
        assert cassette.manifest['truncated'] is True  # Inherited truncation remains.
        assert cassette.manifest['counts'] == {'products': 1}
        assert all(m['value'] == {'bookmarks': {}} for m in output if m['type'] == 'STATE')
    result, resumed = replay_cli(tmp_path, dest, state=output[-1]['value'])
    assert result.returncode == 0 and rows(resumed) == rows(output), result.stderr


@pytest.mark.parametrize('limit', [0, 3])
@pytest.mark.parametrize('corruption', ['excluded-record', 'excluded-state'])
def test_trim_exclude_stream_full_preflight(tmp_path, make_cassette, interleaved, limit, corruption):
    source = make_cassette(interleaved)
    if corruption == 'excluded-record':
        interleaved[-2]['record']['id'] = 'invalid'
    else:
        interleaved[-1]['value']['bookmarks']['audit.events']['replication_key_value'] = 'invalid'
    data = gzip.compress(('\n'.join(json.dumps(m) for m in interleaved) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
    (source / 'manifest.json').write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'invalid'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', limit,
                 '--exclude-stream', 'audit.events', '--compact-states')
    assert result.returncode == 2 and result.stdout == b''
    error = b'schema type' if corruption == 'excluded-record' else b'RFC3339'
    assert error in result.stderr and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_exclude_stream_destination_guards(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'sentinel').write_bytes(b'keep')
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    for dest in [source, alias, existing, source / 'child', alias / 'child']:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1,
                     '--exclude-stream', 'audit.events', '--compact-states')
        assert result.returncode == 2 and result.stdout == b''
    assert not (source / 'child').exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert (existing / 'sentinel').read_bytes() == b'keep'


@pytest.mark.parametrize('excluded,default,overrides,caps,compact', [
    (['events'], 1, [], {'audit.events': 1}, True),
    (['audit.events'], 2, [], {'events': 2}, False),
    (['events'], 0, ['audit.events=2'], {'audit.events': 2}, True),
    (['audit.events'], 2, ['events=0'], {'events': 0}, True),
    (['events'], 4, [], {'audit.events': 4}, False),
])
def test_trim_exclude_stream_contract(tmp_path, make_cassette, interleaved,
                                      excluded, default, overrides, caps, compact):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'excluded'
    options = [arg for s in excluded for arg in ('--exclude-stream', s)]
    options += [arg for value in overrides for arg in ('--stream-limit', value)]
    if compact:
        options.append('--compact-states')
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', default, *options)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    expected = [m for m in rows(interleaved)
                if m['stream'] in caps and m['record']['id'] <= caps[m['stream']]]
    # Independent oracle: preserved source boundaries rebuilt from retained records,
    # then only consecutive equal STATEs removed when requested.
    output_expected, latest = [], {}
    for m in interleaved:
        if m['type'] == 'STATE':
            m = {'type': 'STATE', 'value': {'bookmarks': copy.deepcopy(latest)}}
        elif m['stream'] not in caps:
            continue
        elif m['type'] == 'RECORD':
            if m not in expected:
                continue
            key = 'id' if m['stream'] == 'events' else 'at'
            latest[m['stream']] = {'replication_key': key,
                                   'replication_key_value': m['record'][key]}
        if not (compact and m['type'] == 'STATE' and output_expected and m == output_expected[-1]):
            output_expected.append(m)
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert output == output_expected
        assert rows(output) == expected
        assert output[-1]['value'] == {'bookmarks': latest}
        assert cassette.catalog == {'streams': [entry for entry in json.loads(before['catalog.json'])['streams']
                                                if entry['tap_stream_id'] in caps]}
        assert cassette.manifest['counts'] == {s: min(n, 3) for s, n in caps.items()}
        assert cassette.manifest['message_count'] == len(output)
        assert cassette.manifest['truncated'] is True
        assert cassette.manifest['trim'] == {
            'per_stream': default, 'exclude_streams': sorted(excluded),
            **({'stream_limits': {s: int(n) for s, n in (v.split('=') for v in overrides)}}
               if overrides else {}),
            **({'compact_states': True} if compact else {}),
            'source_sha256': json.loads(before['manifest.json'])['sha256']}
        assert cassette.manifest['recorded_at'] == json.loads(before['manifest.json'])['recorded_at']
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    result, replayed = replay_cli(tmp_path, dest)
    assert result.returncode == 0 and rows(replayed) == expected, result.stderr
    discovery = cli('tap-replay', '--config', tmp_path / 'replay-config.json', '--discover')
    assert discovery.returncode == 0, discovery.stderr
    assert json.loads(discovery.stdout) == cassette.catalog
    for cut in range(len(expected) + 1):
        # Date-time stream checkpoints retain source times across different anchors.
        shifts = (False, False)
        if 'audit.events' in caps and expected:
            shifts = ('2030-01-01T00:00:00Z', '2040-01-01T00:00:00Z')
        result, prefix = replay_cli(tmp_path, dest, fail_after_records=cut, fail_exit_code=19,
                                    time_shift=shifts[0])
        assert result.returncode == 19, result.stderr
        cut_bookmarks = {}
        for m in expected[:cut]:
            key = 'id' if m['stream'] == 'events' else 'at'
            cut_bookmarks[m['stream']] = {'replication_key': key,
                                          'replication_key_value': m['record'][key]}
        assert len(rows(prefix)) == cut and prefix[-1]['value'] == {'bookmarks': cut_bookmarks}
        result, suffix = replay_cli(tmp_path, dest, state=prefix[-1]['value'], time_shift=shifts[1])
        assert result.returncode == 0, result.stderr
        assert [(m['stream'], m['record']['id']) for m in rows(prefix) + rows(suffix)] == [
            (m['stream'], m['record']['id']) for m in expected]
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('maximum,default,options,expected_ids,compact', [
    (3, 2, [], [('events', 1), ('audit.events', 1), ('events', 2)], True),
    (0, 2, [], [], False),
    (1, 2, [], [('events', 1)], False),
    (4, 2, [], [('events', 1), ('audit.events', 1), ('events', 2), ('audit.events', 2)], True),
    (8, 4, [], [(s, n) for n in range(1, 4) for s in ('events', 'audit.events')], False),
    (2, 1, ['--stream-limit', 'events=0', '--stream-limit', 'audit.events=3'],
     [('audit.events', 1), ('audit.events', 2)], True),
    (2, 3, ['--exclude-stream', 'events'], [('audit.events', 1), ('audit.events', 2)], True),
    (1, 3, ['--include-stream', 'audit.events'], [('audit.events', 1)], False),
])
def test_trim_max_records_contract(tmp_path, make_cassette, interleaved, maximum,
                                   default, options, expected_ids, compact):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'global-cap'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', default,
                 '--max-records', maximum, *options, *(['--compact-states'] if compact else []))
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    streams = ['audit.events'] if options and options[0] in ('--include-stream', '--exclude-stream') else [
        'events', 'audit.events']
    expected = [m for m in rows(interleaved) if (m['stream'], m['record']['id']) in expected_ids]
    # Explicit retained IDs are the independent record oracle. Every source boundary
    # must reflect exactly those records seen before it, never a later source cursor.
    wanted, latest = [], {}
    for m in interleaved:
        if m['type'] == 'STATE':
            m = {'type': 'STATE', 'value': {'bookmarks': copy.deepcopy(latest)}}
        elif m['stream'] not in streams:
            continue
        elif m['type'] == 'RECORD':
            if m not in expected:
                continue
            key = 'id' if m['stream'] == 'events' else 'at'
            latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
        if not (compact and wanted and m['type'] == 'STATE' and wanted[-1] == m):
            wanted.append(m)
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert output == wanted
        assert rows(output) == expected
        assert cassette.manifest['counts'] == {s: sum(m['stream'] == s for m in expected) for s in streams}
        assert cassette.manifest['message_count'] == len(wanted)
        assert cassette.manifest['truncated'] is (len(expected) < len(rows(interleaved)))
        provenance = {'per_stream': default, 'max_records': maximum,
                      'source_sha256': json.loads(before['manifest.json'])['sha256']}
        if compact:
            provenance['compact_states'] = True
        if options:
            if options[0] == '--stream-limit':
                provenance['stream_limits'] = {'events': 0, 'audit.events': 3}
            else:
                provenance['include_streams' if options[0] == '--include-stream' else 'exclude_streams'] = [options[1]]
        assert cassette.manifest['trim'] == provenance
        assert cassette.manifest['recorded_at'] == json.loads(before['manifest.json'])['recorded_at']
        assert cassette.catalog == {'streams': [e for e in json.loads(before['catalog.json'])['streams']
                                                if e['tap_stream_id'] in streams]}
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    result, replayed = replay_cli(tmp_path, dest)
    assert result.returncode == 0 and rows(replayed) == expected, result.stderr
    for cut in range(len(expected) + 1):
        shifts = ('2030-01-01T00:00:00Z', '2040-01-01T00:00:00Z') if any(
            m['stream'] == 'audit.events' for m in expected) else (False, False)
        result, prefix = replay_cli(tmp_path, dest, fail_after_records=cut, fail_exit_code=19,
                                    time_shift=shifts[0])
        assert result.returncode == 19 and len(rows(prefix)) == cut, result.stderr
        at_cut = {}
        for m in expected[:cut]:
            key = 'id' if m['stream'] == 'events' else 'at'
            at_cut[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
        assert prefix[-1]['value'] == {'bookmarks': at_cut}
        result, suffix = replay_cli(tmp_path, dest, state=prefix[-1]['value'], time_shift=shifts[1])
        assert result.returncode == 0, result.stderr
        assert [(m['stream'], m['record']['id']) for m in rows(prefix) + rows(suffix)] == expected_ids
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('value', ['-1', '1.5', 'abc', '', 'NaN'])
def test_trim_max_records_invalid_cli(tmp_path, make_cassette, interleaved, value):
    source = make_cassette(interleaved)
    dest = tmp_path / 'invalid-global'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1, '--max-records', value)
    assert result.returncode == 2 and result.stdout == b''
    assert b'max-records' in result.stderr and not dest.exists()


@pytest.mark.parametrize('value', [-1, True, False, 1.0, '1', [], {}])
def test_trim_max_records_invalid_api(tmp_path, make_cassette, interleaved, value):
    source = make_cassette(interleaved)
    dest = tmp_path / 'invalid-global'
    with pytest.raises(ReplayError, match='max-records'):
        trim(source, dest, per_stream=1, max_records=value)
    assert not dest.exists()


def test_trim_max_records_deterministic_and_default_compatibility(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    source_manifest = json.loads((source / 'manifest.json').read_text())
    a, b = tmp_path / 'a', tmp_path / 'b'
    trim(source, a, per_stream=2, compact_states=True)
    trim(source, b, per_stream=2, compact_states=True, max_records=None)
    assert {p.name: p.read_bytes() for p in a.iterdir()} == {p.name: p.read_bytes() for p in b.iterdir()}
    assert 'max_records' not in json.loads((a / 'manifest.json').read_text())['trim']
    c, d = tmp_path / 'c', tmp_path / 'd'
    for dest, opts in [(c, ['--max-records', '0002', '--stream-limit', 'events=0',
                           '--stream-limit', 'audit.events=3']),
                       (d, ['--stream-limit', 'audit.events=03', '--stream-limit', 'events=00',
                            '--max-records', '2'])]:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1, *opts)
        assert result.returncode == 0, result.stderr
    assert {p.name: p.read_bytes() for p in c.iterdir()} == {p.name: p.read_bytes() for p in d.iterdir()}
    inherited = tmp_path / 'inherited'
    trim(c, inherited, per_stream=10, max_records=10)
    with Cassette(inherited) as cassette:
        assert cassette.manifest['truncated'] is True
        assert cassette.manifest['recorded_at'] == source_manifest['recorded_at']
        assert len(rows(list(cassette.messages()))) == 2


@pytest.mark.parametrize('maximum', [0, 1, 10])
def test_trim_max_records_late_schema_and_full_table(tmp_path, make_cassette, maximum):
    schema = lambda s: {'type': 'SCHEMA', 'stream': s, 'key_properties': [],
                         'schema': {'type': 'object', 'properties': {'id': {'type': 'integer'}}}}
    record = lambda s, n: {'type': 'RECORD', 'stream': s, 'record': {'id': n}}
    source = make_cassette([schema('early'), record('early', 1), record('early', 2),
                            schema('late'), record('late', 1)])
    dest = tmp_path / 'late-schema'
    trim(source, dest, per_stream=2, max_records=maximum, compact_states=True)
    expected = [record('early', 1), record('early', 2), record('late', 1)][:maximum]
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert [m['stream'] for m in output if m['type'] == 'SCHEMA'] == ['early', 'late']
        assert rows(output) == expected
        assert output[-1] == {'type': 'STATE', 'value': {'bookmarks': {}}}
        assert cassette.manifest['truncated'] is (maximum < 3)
    result, resumed = replay_cli(tmp_path, dest, state=output[-1]['value'])
    assert result.returncode == 0 and rows(resumed) == expected, result.stderr


@pytest.mark.parametrize('maximum', [0, 1])
@pytest.mark.parametrize('corruption', ['excluded-record', 'state'])
def test_trim_max_records_preflight(tmp_path, make_cassette, interleaved, maximum, corruption):
    source = make_cassette(interleaved)
    if corruption == 'excluded-record':
        interleaved[-2]['record']['id'] = 'invalid'
    else:
        interleaved[-1]['value']['bookmarks']['audit.events']['replication_key_value'] = 'invalid'
    data = gzip.compress(('\n'.join(json.dumps(m) for m in interleaved) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
    (source / 'manifest.json').write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'invalid-source'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2,
                 '--exclude-stream', 'audit.events', '--max-records', maximum, '--compact-states')
    assert result.returncode == 2 and result.stdout == b''
    assert b'unrecognized arguments' not in result.stderr and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_max_records_zero_preserves_guards(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'sentinel').write_bytes(b'keep')
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    for dest in [source, alias, existing, source / 'child']:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1, '--max-records', 0)
        assert result.returncode == 2 and result.stdout == b''
        assert b'unrecognized arguments' not in result.stderr
    for options in [('--exclude-stream', 'unknown'), ('--include-stream', 'events', '--exclude-stream', 'audit.events'),
                    ('--exclude-stream', 'audit.events', '--stream-limit', 'audit.events=0')]:
        dest = tmp_path / 'bad-options'
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1,
                     '--max-records', 0, *options)
        assert result.returncode == 2 and result.stdout == b'' and not dest.exists()
    assert not (source / 'child').exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert (existing / 'sentinel').read_bytes() == b'keep'


@pytest.mark.parametrize('compact', [False, True])
def test_trim_drop_empty_streams_contract(tmp_path, make_cassette, compact):
    # Concrete round-10 contract: the global cap starves beta, so its schema disappears.
    schema = lambda s: {'type': 'SCHEMA', 'stream': s, 'key_properties': ['id'],
                        'replication_key': 'id', 'schema': {'type': 'object',
                        'properties': {'id': {'type': 'integer'}}}}
    alpha, beta = schema('alpha'), schema('beta')
    bookmark = lambda s: {s: {'replication_key': 'id', 'replication_key_value': 1}}
    a = {'type': 'RECORD', 'stream': 'alpha', 'record': {'id': 1}}
    b = {'type': 'RECORD', 'stream': 'beta', 'record': {'id': 1}}
    source = make_cassette([alpha, beta, a,
        {'type': 'STATE', 'value': {'bookmarks': bookmark('alpha')}}, b,
        {'type': 'STATE', 'value': {'bookmarks': {**bookmark('alpha'), **bookmark('beta')}}}])
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'nonempty'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2,
                 '--max-records', 1, '--drop-empty-streams', *(['--compact-states'] if compact else []))
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    state = {'type': 'STATE', 'value': {'bookmarks': bookmark('alpha')}}
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert output == [alpha, a, state] + ([] if compact else [state])
        assert cassette.manifest['counts'] == {'alpha': 1}
        assert cassette.manifest['message_count'] == len(output)
        assert cassette.manifest['truncated'] is True
        assert cassette.manifest['trim']['drop_empty_streams'] is True
        assert cassette.manifest['trim']['source_sha256'] == json.loads(before['manifest.json'])['sha256']
        assert [e['tap_stream_id'] for e in cassette.catalog['streams']] == ['alpha']
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    result, replayed = replay_cli(tmp_path, dest)
    assert result.returncode == 0 and rows(replayed) == [a], result.stderr
    result = cli('tap-replay', '--config', tmp_path / 'replay-config.json', '--discover')
    assert result.returncode == 0, result.stderr
    assert [e['tap_stream_id'] for e in json.loads(result.stdout)['streams']] == ['alpha']


@pytest.mark.parametrize('options,expected', [
    ({'per_stream': 2, 'max_records': 1}, [('events', 1)]),
    ({'per_stream': 0, 'stream_limits': {'audit.events': 2}}, [('audit.events', 1), ('audit.events', 2)]),
    ({'per_stream': 2, 'stream_limits': {'events': 0}, 'max_records': 1}, [('audit.events', 1)]),
    ({'per_stream': 2, 'include_streams': ['audit.events', 'events'], 'max_records': 1}, [('events', 1)]),
    ({'per_stream': 2, 'exclude_streams': ['events'], 'max_records': 2}, [('audit.events', 1), ('audit.events', 2)]),
    ({'per_stream': 10}, [('events', 1), ('audit.events', 1), ('events', 2),
                          ('audit.events', 2), ('events', 3), ('audit.events', 3)]),
])
def test_trim_drop_empty_streams_selection_caps(tmp_path, make_cassette, interleaved, options, expected):
    empty = copy.deepcopy(interleaved[1])
    empty['stream'] = 'schema.only'
    source = make_cassette([*interleaved, empty, empty])
    dest = tmp_path / 'selected'
    trim(source, dest, drop_empty_streams=True, **options)
    streams = {s for s, _ in expected}
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert [(m['stream'], m['record']['id']) for m in rows(output)] == expected
        assert [m for m in output if m['type'] == 'SCHEMA'] == [
            m for m in interleaved if m['type'] == 'SCHEMA' and m['stream'] in streams]
        assert set(cassette.manifest['counts']) == streams
        assert {e['tap_stream_id'] for e in cassette.catalog['streams']} == streams
        assert cassette.manifest['truncated'] is (len(expected) < 6)
        latest = {}
        for m in output:
            if m['type'] == 'RECORD':
                key = 'id' if m['stream'] == 'events' else 'at'
                latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
            elif m['type'] == 'STATE':
                assert m['value'] == {'bookmarks': latest}
        assert output[-1] == {'type': 'STATE', 'value': {'bookmarks': latest}}


@pytest.mark.parametrize('compact', [False, True])
def test_trim_drop_empty_streams_late_schemas_and_compaction(tmp_path, make_cassette, interleaved, compact):
    schema = interleaved[1]
    empty = copy.deepcopy(schema)
    empty['stream'] = 'empty'
    record = {'type': 'RECORD', 'stream': 'events', 'record': {'id': 1}}
    state = {'type': 'STATE', 'value': {'bookmarks': {}}}
    # Removing an empty schema makes these rebuilt states adjacent; a retained schema is a barrier.
    source = make_cassette([schema, record, state, empty, state, schema, state, empty])
    dest = tmp_path / 'late'
    trim(source, dest, per_stream=1, drop_empty_streams=True, compact_states=compact)
    rebuilt = {'type': 'STATE', 'value': {'bookmarks': {
        'events': {'replication_key': 'id', 'replication_key_value': 1}}}}
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == [schema, record, rebuilt] + (
            [] if compact else [rebuilt]) + [schema, rebuilt]
        assert cassette.manifest['truncated'] is False


@pytest.mark.parametrize('fail_after', range(4))
@pytest.mark.parametrize('compact', [False, True])
def test_trim_drop_empty_streams_keyed_resume(tmp_path, make_cassette, interleaved, fail_after, compact):
    empty = copy.deepcopy(interleaved[1])
    empty['stream'] = 'empty'
    source = make_cassette([empty, *interleaved])
    dest = tmp_path / 'resumable'
    trim(source, dest, per_stream=2, max_records=3, drop_empty_streams=True, compact_states=compact)
    result, before = replay_cli(tmp_path, dest, fail_after_records=fail_after, fail_exit_code=19,
                                time_shift='2030-01-01T00:00:00Z', state_message_frequency=1)
    assert result.returncode == 19, result.stderr
    latest = {}
    expected = rows(interleaved)[:3]
    for m in expected[:fail_after]:
        key = 'id' if m['stream'] == 'events' else 'at'
        latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
    assert before[-1]['value'] == {'bookmarks': latest}
    result, after = replay_cli(tmp_path, dest, state=before[-1]['value'],
                              time_shift='2040-01-01T00:00:00Z')
    assert result.returncode == 0, result.stderr
    assert [(m['stream'], m['record']['id']) for m in rows(before) + rows(after)] == [
        (m['stream'], m['record']['id']) for m in expected]
    assert 'empty' not in after[-1]['value']['bookmarks']
    result, final = replay_cli(tmp_path, dest, state=after[-1]['value'])
    assert result.returncode == 0 and rows(final) == [], result.stderr


def test_trim_drop_empty_streams_compatibility_and_determinism(tmp_path, make_cassette, messages):
    source = make_cassette(messages, rules=scrub_rules(['customers.email=hash']), salt=b'T' * 32)
    a, b, c, d = [tmp_path / s for s in 'abcd']
    kwargs = {'per_stream': 2, 'stream_limits': {'customers': 0, 'products': 1}, 'max_records': 1}
    trim(source, a, **kwargs)
    trim(source, b, **kwargs, drop_empty_streams=False)
    artifacts = lambda path: {p.name: p.read_bytes() for p in path.iterdir()}
    assert artifacts(a) == artifacts(b)
    source_manifest = json.loads((source / 'manifest.json').read_text())
    with Cassette(a) as cassette:
        assert cassette.manifest['counts'] == {'customers': 0, 'products': 1}
        assert 'drop_empty_streams' not in cassette.manifest['trim']
    for dest, options in [(c, ['--drop-empty-streams', '--stream-limit', 'customers=00',
                              '--stream-limit', 'products=01', '--max-records', '01']),
                          (d, ['--max-records', '1', '--stream-limit', 'products=1',
                              '--stream-limit', 'customers=0', '--drop-empty-streams'])]:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2, *options)
        assert result.returncode == 0 and result.stdout == b'', result.stderr
    assert artifacts(c) == artifacts(d)
    with Cassette(c) as cassette:
        output = list(cassette.messages())
        assert cassette.manifest['counts'] == {'products': 1}
        assert cassette.manifest['scrub'] == source_manifest['scrub']
        assert cassette.manifest['recorded_at'] == source_manifest['recorded_at']
        assert output[-1]['value'] == {'bookmarks': {}}
    result, resumed = replay_cli(tmp_path, c, state=output[-1]['value'])
    assert result.returncode == 0 and rows(resumed) == rows(output), result.stderr
    inherited = tmp_path / 'inherited'
    trim(c, inherited, per_stream=10, drop_empty_streams=True)
    with Cassette(inherited) as cassette:
        assert cassette.manifest['truncated'] is True


@pytest.mark.parametrize('options', [
    ['--per-stream', '0'], ['--per-stream', '3', '--max-records', '0'],
    ['--per-stream', '3', '--stream-limit', 'events=0', '--stream-limit', 'audit.events=0'],
    ['--per-stream', '0', '--include-stream', 'events'],
    ['--per-stream', '0', '--exclude-stream', 'audit.events'],
])
def test_trim_drop_empty_streams_all_empty_rejected(tmp_path, make_cassette, interleaved, options):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'all-empty'
    result = cli('singer-replay', 'trim', source, '-o', dest, *options, '--drop-empty-streams')
    assert result.returncode == 2 and result.stdout == b''
    assert b'at least one schema' in result.stderr and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_drop_empty_streams_schema_only_rejected(tmp_path, make_cassette, interleaved):
    source = make_cassette([m for m in interleaved if m['type'] != 'RECORD' and m['type'] != 'STATE'])
    dest = tmp_path / 'schema-only'
    with pytest.raises(ReplayError, match='at least one schema'):
        trim(source, dest, per_stream=10, drop_empty_streams=True)
    assert not dest.exists()


@pytest.mark.parametrize('bad', [None, 0, 1, 'true', [], {}])
def test_trim_drop_empty_streams_strict_boolean(tmp_path, make_cassette, interleaved, bad):
    source = make_cassette(interleaved)
    dest = tmp_path / 'bad-boolean'
    with pytest.raises(ReplayError, match='drop-empty-streams must be boolean'):
        trim(source, dest, per_stream=1, drop_empty_streams=bad)
    assert not dest.exists()


@pytest.mark.parametrize('maximum', [0, 1])
@pytest.mark.parametrize('corruption,error', [('excluded-record', b'schema type'), ('state', b'RFC3339'),
                                            ('checksum', b'checksum')])
def test_trim_drop_empty_streams_full_preflight(tmp_path, make_cassette, interleaved, maximum, corruption, error):
    source = make_cassette(interleaved)
    if corruption == 'excluded-record':
        interleaved[-2]['record']['id'] = 'invalid'
    elif corruption == 'state':
        interleaved[-1]['value']['bookmarks']['audit.events']['replication_key_value'] = 'invalid'
    data = gzip.compress(('\n'.join(json.dumps(m) for m in interleaved) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    if corruption != 'checksum':
        manifest = json.loads((source / 'manifest.json').read_text())
        manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
        (source / 'manifest.json').write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'invalid'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2,
                 '--exclude-stream', 'audit.events', '--max-records', maximum, '--drop-empty-streams')
    assert result.returncode == 2 and result.stdout == b''
    assert error in result.stderr and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_drop_empty_streams_zero_preserves_guards_and_validation(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'sentinel').write_bytes(b'keep')
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    for dest in [source, alias, existing, source / 'child', alias / 'child']:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0, '--drop-empty-streams')
        assert result.returncode == 2 and result.stdout == b''
        assert b'overwrite' in result.stderr or b'outside the source' in result.stderr
    for options, error in [
        (['--include-stream', 'unknown'], b'unknown stream'),
        (['--exclude-stream', 'events', '--exclude-stream', 'events'], b'duplicate'),
        (['--exclude-stream', 'audit.events', '--stream-limit', 'audit.events=0'], b'excluded stream'),
        (['--exclude-stream', 'audit.events', '--exclude-stream', 'events'], b'cannot exclude every'),
        (['--include-stream', 'events', '--exclude-stream', 'audit.events'], b'not allowed'),
        (['--stream-limit', 'events=-1'], b'stream-limit'),
        (['--max-records', '-1'], b'max-records'),
        (['--drop-empty-streams=false'], b'ignored explicit argument'),
    ]:
        dest = tmp_path / 'bad-options'
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0,
                     '--drop-empty-streams', *options)
        assert result.returncode == 2 and result.stdout == b''
        assert error in result.stderr and not dest.exists()
    assert not (source / 'child').exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert (existing / 'sentinel').read_bytes() == b'keep'


@pytest.mark.parametrize('compact', [False, True])
def test_trim_final_state_only_contract(tmp_path, make_cassette, compact):
    schema = lambda s: {'type': 'SCHEMA', 'stream': s, 'key_properties': ['id'],
                        'replication_key': 'id', 'schema': {'type': 'object',
                        'properties': {'id': {'type': 'integer'}}}}
    record = lambda s, n: {'type': 'RECORD', 'stream': s, 'record': {'id': n}}
    state = lambda b: {'type': 'STATE', 'value': {'bookmarks': copy.deepcopy(b)}}
    source_messages = [state({}), schema('alpha'), schema('beta')]
    bookmarks = {}
    for n in (1, 2):
        for s in ('alpha', 'beta'):
            source_messages.append(record(s, n))
            bookmarks[s] = {'replication_key': 'id', 'replication_key_value': n}
            source_messages.append(state(bookmarks))
    source = make_cassette(source_messages)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'final-only'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1,
                 '--final-state-only', *(['--compact-states'] if compact else []))
    assert result.returncode == 0 and result.stdout == b'', result.stderr
    expected = [schema('alpha'), schema('beta'), record('alpha', 1), record('beta', 1),
                state({s: {'replication_key': 'id', 'replication_key_value': 1}
                       for s in ('alpha', 'beta')})]
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == expected
        assert cassette.manifest['counts'] == {'alpha': 1, 'beta': 1}
        assert cassette.manifest['message_count'] == 5
        assert cassette.manifest['truncated'] is True
        assert cassette.manifest['trim'] == {
            'per_stream': 1, 'source_sha256': json.loads(before['manifest.json'])['sha256'],
            'final_state_only': True, **({'compact_states': True} if compact else {})}
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('options,expected', [
    ({'per_stream': 0}, []),
    ({'per_stream': 9}, [('events', 1), ('audit.events', 1), ('events', 2),
                         ('audit.events', 2), ('events', 3), ('audit.events', 3)]),
    ({'per_stream': 2, 'max_records': 0}, []),
    ({'per_stream': 2, 'max_records': 3}, [('events', 1), ('audit.events', 1), ('events', 2)]),
    ({'per_stream': 0, 'stream_limits': {'audit.events': 2}, 'max_records': 1}, [('audit.events', 1)]),
    ({'per_stream': 2, 'include_streams': ['audit.events'], 'max_records': 1}, [('audit.events', 1)]),
    ({'per_stream': 2, 'exclude_streams': ['audit.events'], 'max_records': 1}, [('events', 1)]),
    ({'per_stream': 2, 'stream_limits': {'events': 0}, 'drop_empty_streams': True},
     [('audit.events', 1), ('audit.events', 2)]),
    ({'per_stream': 2, 'max_records': 1, 'drop_empty_streams': True}, [('events', 1)]),
])
def test_trim_final_state_only_selection(tmp_path, make_cassette, interleaved, options, expected):
    source = make_cassette(interleaved)
    dest = tmp_path / 'selection'
    trim(source, dest, final_state_only=True, compact_states=True, **options)
    chosen = set(options.get('include_streams', ['events', 'audit.events'])) - set(options.get('exclude_streams', []))
    if options.get('drop_empty_streams'):
        chosen &= {s for s, _ in expected}
    expected_rows = [m for m in rows(interleaved) if (m['stream'], m['record']['id']) in expected]
    latest = {}
    for m in expected_rows:
        key = 'id' if m['stream'] == 'events' else 'at'
        latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert output == [m for m in interleaved if m['type'] == 'SCHEMA' and m['stream'] in chosen] + expected_rows + [
            {'type': 'STATE', 'value': {'bookmarks': latest}}]
        assert cassette.manifest['counts'] == {s: sum(s == stream for stream, _ in expected) for s in chosen}
        assert cassette.manifest['message_count'] == len(output)
        assert cassette.manifest['truncated'] is (len(expected) < 6)
        assert {e['stream'] for e in cassette.catalog['streams']} == chosen
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('source_states', [False, True])
@pytest.mark.parametrize('zero', [False, True])
def test_trim_final_state_only_late_schemas(tmp_path, make_cassette, interleaved, source_states, zero):
    schema = interleaved[1]
    empty = copy.deepcopy(schema)
    empty['stream'] = 'empty'
    record = {'type': 'RECORD', 'stream': 'events', 'record': {'id': 1}}
    boundary = {'type': 'STATE', 'value': {'bookmarks': {}}}
    messages = [schema, record, schema, empty]
    if source_states:
        messages = [boundary, schema, boundary, record, boundary, schema, boundary, empty]
    source = make_cassette(messages)
    dest = tmp_path / 'late'
    trim(source, dest, per_stream=0 if zero else 1, final_state_only=True)
    latest = {} if zero else {'events': {'replication_key': 'id', 'replication_key_value': 1}}
    expected = [schema] + ([] if zero else [record]) + [schema, empty,
                {'type': 'STATE', 'value': {'bookmarks': latest}}]
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == expected
        assert cassette.manifest['truncated'] is zero


def test_trim_final_state_only_schema_only_input(tmp_path, make_cassette, interleaved):
    schemas = [m for m in interleaved if m['type'] == 'SCHEMA']
    source = make_cassette(schemas)
    dest = tmp_path / 'schemas'
    trim(source, dest, per_stream=10, final_state_only=True)
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == schemas + [{'type': 'STATE', 'value': {'bookmarks': {}}}]
        assert cassette.manifest['message_count'] == 3
        assert cassette.manifest['truncated'] is False


@pytest.mark.parametrize('fail_after', range(4))
@pytest.mark.parametrize('compact', [False, True])
def test_trim_final_state_only_keyed_resume(tmp_path, make_cassette, interleaved, fail_after, compact):
    source = make_cassette(interleaved)
    dest = tmp_path / 'resumable'
    trim(source, dest, per_stream=2, max_records=3, final_state_only=True,
         drop_empty_streams=True, compact_states=compact)
    expected = rows(interleaved)[:3]
    # At frequency=0 only the injected failure can create a checkpoint before the final state.
    result, before = replay_cli(tmp_path, dest, fail_after_records=fail_after, fail_exit_code=19,
                                time_shift='2030-01-01T00:00:00Z', state_message_frequency=0)
    assert result.returncode == 19, result.stderr
    assert len(rows(before)) == fail_after
    latest = {}
    for m in expected[:fail_after]:
        key = 'id' if m['stream'] == 'events' else 'at'
        latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
    assert before[-1] == {'type': 'STATE', 'value': {'bookmarks': latest}}
    result, after = replay_cli(tmp_path, dest, state=before[-1]['value'],
                              time_shift='2040-01-01T00:00:00Z', state_message_frequency=1)
    assert result.returncode == 0, result.stderr
    assert [(m['stream'], m['record']['id']) for m in rows(before) + rows(after)] == [
        (m['stream'], m['record']['id']) for m in expected]
    shifted = rows(before) + rows(after)
    assert [m for m in shifted if m['stream'] == 'audit.events'][0]['record']['at'] != expected[1]['record']['at']
    result, final = replay_cli(tmp_path, dest, state=after[-1]['value'])
    assert result.returncode == 0 and rows(final) == [], result.stderr


def test_trim_final_state_only_compatibility_determinism_and_full_table(tmp_path, make_cassette, messages):
    source = make_cassette(messages, rules=scrub_rules(['customers.email=hash']), salt=b'T' * 32)
    artifacts = lambda path: {p.name: p.read_bytes() for p in path.iterdir()}
    before = artifacts(source)
    a, b, c, d = [tmp_path / name for name in 'abcd']
    kwargs = {'per_stream': 2, 'stream_limits': {'customers': 0, 'products': 1}, 'max_records': 1}
    trim(source, a, **kwargs)
    trim(source, b, **kwargs, final_state_only=False)
    assert artifacts(a) == artifacts(b)
    assert 'final_state_only' not in json.loads((a / 'manifest.json').read_text())['trim']
    for dest, flags in [(c, ['--final-state-only', '--compact-states', '--drop-empty-streams',
                            '--stream-limit', 'customers=00', '--stream-limit', 'products=01', '--max-records', '01']),
                        (d, ['--max-records', '1', '--stream-limit', 'products=1', '--stream-limit', 'customers=0',
                            '--drop-empty-streams', '--compact-states', '--final-state-only'])]:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2, *flags)
        assert result.returncode == 0 and result.stdout == b'', result.stderr
    assert artifacts(c) == artifacts(d)
    source_manifest = json.loads(before['manifest.json'])
    with Cassette(c) as cassette:
        output = list(cassette.messages())
        assert output[-1] == {'type': 'STATE', 'value': {'bookmarks': {}}}
        assert sum(m['type'] == 'STATE' for m in output) == 1
        assert cassette.manifest['counts'] == {'products': 1}
        assert cassette.manifest['recorded_at'] == source_manifest['recorded_at']
        assert cassette.manifest['scrub'] == source_manifest['scrub']
        assert cassette.manifest['trim']['source_sha256'] == source_manifest['sha256']
    result, resumed = replay_cli(tmp_path, c, state=output[-1]['value'])
    assert result.returncode == 0 and rows(resumed) == rows(output), result.stderr
    inherited = tmp_path / 'inherited'
    trim(c, inherited, per_stream=10, final_state_only=True)
    with Cassette(inherited) as cassette:
        assert cassette.manifest['truncated'] is True
    assert artifacts(source) == before


@pytest.mark.parametrize('bad', [None, 0, 1, 'true', [], {}])
def test_trim_final_state_only_strict_boolean(tmp_path, make_cassette, interleaved, bad):
    source = make_cassette(interleaved)
    dest = tmp_path / 'bad-bool'
    with pytest.raises(ReplayError, match='final-state-only must be boolean'):
        trim(source, dest, per_stream=1, final_state_only=bad)
    assert not dest.exists()


@pytest.mark.parametrize('boundaries', [['--final-state-only'], ['--state-every', '2']])
@pytest.mark.parametrize('maximum', [0, 1])
@pytest.mark.parametrize('corruption,error', [('excluded-record', b'schema type'),
    ('discarded-record', b'schema type'), ('state', b'RFC3339'), ('checksum', b'checksum')])
def test_trim_replaced_boundaries_full_preflight(tmp_path, make_cassette, interleaved, maximum, corruption, error, boundaries):
    source = make_cassette(interleaved)
    if corruption in ('excluded-record', 'discarded-record'):
        interleaved[-2 if corruption == 'excluded-record' else -4]['record']['id'] = 'invalid'
    elif corruption == 'state':
        interleaved[-1]['value']['bookmarks']['audit.events']['replication_key_value'] = 'invalid'
    data = gzip.compress(('\n'.join(json.dumps(m) for m in interleaved) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    if corruption != 'checksum':
        manifest = json.loads((source / 'manifest.json').read_text())
        manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
        (source / 'manifest.json').write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'invalid'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1,
                 '--exclude-stream', 'audit.events', '--max-records', maximum,
                 *boundaries, '--drop-empty-streams')
    assert result.returncode == 2 and result.stdout == b'', result.stderr
    assert error in result.stderr and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('boundaries', [['--final-state-only'], ['--state-every', '2']])
def test_trim_replaced_boundaries_zero_guards_and_validation(tmp_path, make_cassette, interleaved, boundaries):
    source = make_cassette(interleaved)
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'sentinel').write_bytes(b'keep')
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    for dest in [source, alias, existing, source / 'child', alias / 'child']:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0, *boundaries)
        assert result.returncode == 2 and result.stdout == b''
        assert b'overwrite' in result.stderr or b'outside the source' in result.stderr
    for options, error in [
        (['--include-stream', 'unknown'], b'unknown stream'),
        (['--exclude-stream', 'events', '--exclude-stream', 'events'], b'duplicate'),
        (['--exclude-stream', 'audit.events', '--stream-limit', 'audit.events=0'], b'excluded stream'),
        (['--include-stream', 'events', '--exclude-stream', 'audit.events'], b'not allowed'),
        (['--stream-limit', 'events=-1'], b'stream-limit'),
        (['--max-records', '-1'], b'max-records'),
        (['--state-every', '0'], b'state-every') if boundaries[0] == '--state-every' else
        (['--final-state-only=false'], b'ignored explicit argument'),
        (['--drop-empty-streams'], b'at least one schema'),
    ]:
        dest = tmp_path / 'bad-options'
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0,
                     *boundaries, *options)
        assert result.returncode == 2 and result.stdout == b''
        assert error in result.stderr and not dest.exists()
    assert not (source / 'child').exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert (existing / 'sentinel').read_bytes() == b'keep'


@pytest.mark.parametrize('compact', [False, True])
def test_trim_state_every_contract(tmp_path, make_cassette, compact):
    schema = lambda s: {'type': 'SCHEMA', 'stream': s, 'key_properties': ['id'],
                        'replication_key': 'id', 'schema': {'type': 'object',
                        'properties': {'id': {'type': 'integer'}}}}
    record = lambda s, n: {'type': 'RECORD', 'stream': s, 'record': {'id': n}}
    state = lambda n: {'type': 'STATE', 'value': {'bookmarks': {
        s: {'replication_key': 'id', 'replication_key_value': n} for s in ('alpha', 'beta')}}}
    empty = {'type': 'STATE', 'value': {'bookmarks': {}}}
    messages = [empty, schema('alpha'), empty, schema('beta')]
    for n in (1, 2, 3):
        messages += [record('alpha', n), empty, empty, record('beta', n)]
    messages += [empty, empty]
    source = make_cassette(messages)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'cadence'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2,
                 '--state-every', '02', *(['--compact-states'] if compact else []))
    assert result.returncode == 0 and result.stdout == b'', result.stderr
    expected = [schema('alpha'), schema('beta'), record('alpha', 1), record('beta', 1), state(1),
                record('alpha', 2), record('beta', 2), state(2)]
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == expected
        assert cassette.manifest['counts'] == {'alpha': 2, 'beta': 2}
        assert cassette.manifest['message_count'] == 8
        assert cassette.manifest['truncated'] is True
        assert cassette.manifest['trim'] == {
            'per_stream': 2, 'source_sha256': json.loads(before['manifest.json'])['sha256'],
            'state_every': 2, **({'compact_states': True} if compact else {})}
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('interval', [1, 2, 3, 99])
@pytest.mark.parametrize('options,ids', [
    ({'per_stream': 0}, []),
    ({'per_stream': 9}, [('events', 1), ('audit.events', 1), ('events', 2),
                         ('audit.events', 2), ('events', 3), ('audit.events', 3)]),
    ({'per_stream': 2, 'max_records': 0}, []),
    ({'per_stream': 2, 'max_records': 3}, [('events', 1), ('audit.events', 1), ('events', 2)]),
    ({'per_stream': 0, 'stream_limits': {'audit.events': 2}, 'max_records': 1}, [('audit.events', 1)]),
    ({'per_stream': 2, 'include_streams': ['audit.events']}, [('audit.events', 1), ('audit.events', 2)]),
    ({'per_stream': 2, 'exclude_streams': ['audit.events']}, [('events', 1), ('events', 2)]),
    ({'per_stream': 2, 'stream_limits': {'events': 0}, 'drop_empty_streams': True},
     [('audit.events', 1), ('audit.events', 2)]),
    ({'per_stream': 2, 'max_records': 1, 'drop_empty_streams': True}, [('events', 1)]),
])
def test_trim_state_every_selection(tmp_path, make_cassette, interleaved, interval, options, ids):
    source = make_cassette(interleaved)
    dest = tmp_path / 'selection'
    trim(source, dest, state_every=interval, compact_states=True, **options)
    chosen = set(options.get('include_streams', ['events', 'audit.events'])) - set(options.get('exclude_streams', []))
    if options.get('drop_empty_streams'):
        chosen &= {s for s, _ in ids}
    expected = [m for m in interleaved if m['type'] == 'SCHEMA' and m['stream'] in chosen]
    latest, kept = {}, 0
    for m in rows(interleaved):
        if (m['stream'], m['record']['id']) not in ids:
            continue
        expected.append(m)
        key = 'id' if m['stream'] == 'events' else 'at'
        latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
        kept += 1
        if kept % interval == 0:
            expected.append({'type': 'STATE', 'value': {'bookmarks': copy.deepcopy(latest)}})
    if expected[-1]['type'] != 'STATE':
        expected.append({'type': 'STATE', 'value': {'bookmarks': latest}})
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == expected
        assert cassette.manifest['message_count'] == len(expected)
        assert cassette.manifest['counts'] == {s: sum(s == stream for stream, _ in ids) for s in chosen}
        assert cassette.manifest['truncated'] is (len(ids) < 6)
        assert {e['stream'] for e in cassette.catalog['streams']} == chosen


@pytest.mark.parametrize('interval', [1, 2, 10])
@pytest.mark.parametrize('zero', [False, True])
def test_trim_state_every_trailing_schemas(tmp_path, make_cassette, interleaved, interval, zero):
    schema = interleaved[1]
    empty = copy.deepcopy(schema)
    empty['stream'] = 'empty'
    record = {'type': 'RECORD', 'stream': 'events', 'record': {'id': 1}}
    boundary = {'type': 'STATE', 'value': {'bookmarks': {}}}
    source = make_cassette([boundary, schema, record, boundary, schema, empty, boundary])
    dest = tmp_path / 'late'
    trim(source, dest, per_stream=0 if zero else 1, state_every=interval)
    state = {'type': 'STATE', 'value': {'bookmarks': {} if zero else {
        'events': {'replication_key': 'id', 'replication_key_value': 1}}}}
    expected = [schema] + ([] if zero else [record] + ([state] if interval == 1 else [])) + [schema, empty, state]
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == expected
        assert cassette.manifest['truncated'] is zero
    schemas = make_cassette([schema, empty])
    trim(schemas, tmp_path / 'schema-only', per_stream=9, state_every=interval)
    with Cassette(tmp_path / 'schema-only') as cassette:
        assert list(cassette.messages()) == [schema, empty, boundary]
        assert cassette.manifest['truncated'] is False


@pytest.mark.parametrize('interval', [1, 3])
@pytest.mark.parametrize('fail_after', range(5))
def test_trim_state_every_keyed_resume(tmp_path, make_cassette, interleaved, interval, fail_after):
    source = make_cassette(interleaved)
    dest = tmp_path / 'resumable'
    trim(source, dest, per_stream=2, state_every=interval, drop_empty_streams=True)
    expected = rows(interleaved)[:4]
    result, before = replay_cli(tmp_path, dest, fail_after_records=fail_after, fail_exit_code=19,
                                time_shift='2030-01-01T00:00:00Z', state_message_frequency=0)
    assert result.returncode == 19, result.stderr
    assert len(rows(before)) == fail_after
    latest = {}
    for m in expected[:fail_after]:
        key = 'id' if m['stream'] == 'events' else 'at'
        latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
    assert before[-1] == {'type': 'STATE', 'value': {'bookmarks': latest}}
    result, after = replay_cli(tmp_path, dest, state=before[-1]['value'],
                              time_shift='2040-01-01T00:00:00Z', state_message_frequency=1)
    assert result.returncode == 0, result.stderr
    assert [(m['stream'], m['record']['id']) for m in rows(before) + rows(after)] == [
        (m['stream'], m['record']['id']) for m in expected]
    assert all(m['record']['at'] != expected[1]['record']['at']
               for m in rows(before) + rows(after) if m['stream'] == 'audit.events')
    result, final = replay_cli(tmp_path, dest, state=after[-1]['value'])
    assert result.returncode == 0 and rows(final) == [], result.stderr


def test_trim_state_every_compatibility_determinism_full_table(tmp_path, make_cassette, messages):
    source = make_cassette(messages, rules=scrub_rules(['customers.email=hash']), salt=b'T' * 32)
    artifacts = lambda p: {f.name: f.read_bytes() for f in p.iterdir()}
    before = artifacts(source)
    a, b, c, d = [tmp_path / n for n in 'abcd']
    trim(source, a, per_stream=2, compact_states=True)
    trim(source, b, per_stream=2, compact_states=True, state_every=None)
    assert artifacts(a) == artifacts(b)
    assert 'state_every' not in json.loads((a / 'manifest.json').read_text())['trim']
    for dest, flags in [(c, ['--state-every', '01', '--compact-states', '--drop-empty-streams',
                            '--stream-limit', 'customers=00', '--stream-limit', 'products=01']),
                        (d, ['--stream-limit', 'products=1', '--stream-limit', 'customers=0',
                            '--drop-empty-streams', '--compact-states', '--state-every', '1'])]:
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2, *flags)
        assert result.returncode == 0 and result.stdout == b'', result.stderr
    assert artifacts(c) == artifacts(d)
    with Cassette(c) as cassette:
        output = list(cassette.messages())
        assert [m['type'] for m in output] == ['SCHEMA', 'RECORD', 'STATE']
        assert output[-1]['value'] == {'bookmarks': {}}
        assert cassette.manifest['counts'] == {'products': 1}
        assert cassette.manifest['message_count'] == 3
        assert cassette.manifest['scrub'] == json.loads(before['manifest.json'])['scrub']
        assert cassette.manifest['recorded_at'] == json.loads(before['manifest.json'])['recorded_at']
    result, repeated = replay_cli(tmp_path, c, state=output[-1]['value'])
    assert result.returncode == 0 and rows(repeated) == rows(output), result.stderr
    # Boundary changes alone do not truncate, but inherited truncation survives.
    truncated = make_cassette(messages, limit=2)
    trim(truncated, tmp_path / 'inherited', per_stream=9, state_every=1)
    with Cassette(tmp_path / 'inherited') as cassette:
        assert cassette.manifest['truncated'] is True
    assert artifacts(source) == before


@pytest.mark.parametrize('bad', [0, -1, 1.5, '2', True, False, [], {}])
def test_trim_state_every_invalid_api(tmp_path, make_cassette, interleaved, bad):
    source = make_cassette(interleaved)
    dest = tmp_path / 'bad'
    with pytest.raises(ReplayError, match='state-every must be a positive integer'):
        trim(source, dest, per_stream=1, state_every=bad)
    assert not dest.exists()


def test_trim_state_every_api_conflict_and_bool_contracts(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    dest = tmp_path / 'bad'
    for options, error in [({'final_state_only': True}, 'cannot be combined'),
                           ({'compact_states': 1}, 'compact-states must be boolean'),
                           ({'final_state_only': 0}, 'final-state-only must be boolean'),
                           ({'drop_empty_streams': 1}, 'drop-empty-streams must be boolean')]:
        with pytest.raises(ReplayError, match=error):
            trim(source, dest, per_stream=1, state_every=2, **options)
        assert not dest.exists()


@pytest.mark.parametrize('flags', [
    ['0'], ['-1'], ['1.5'], ['true'], [''], [],
    ['2', '--final-state-only'], ['2', '--max-records', '-1'],
    ['2', '--stream-limit', 'events=-1'],
    ['2', '--include-stream', 'unknown'], ['2', '--exclude-stream', 'events', '--exclude-stream', 'events'],
    ['2', '--include-stream', 'events', '--stream-limit', 'audit.events=0'],
    ['2', '--include-stream', 'events', '--exclude-stream', 'audit.events'],
])
def test_trim_state_every_invalid_cli(tmp_path, make_cassette, interleaved, flags):
    source = make_cassette(interleaved)
    dest = tmp_path / 'invalid'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1, '--state-every', *flags)
    assert result.returncode == 2 and result.stdout == b''
    assert not dest.exists()


@pytest.mark.parametrize('compact', [False, True])
def test_trim_schema_once_exact_contract(tmp_path, make_cassette, compact):
    schema = {'type': 'SCHEMA', 'stream': 'alpha', 'key_properties': ['id'],
              'replication_key': 'id', 'schema': {'type': 'object',
              'properties': {'id': {'type': 'integer'}}}}
    record = lambda n: {'type': 'RECORD', 'stream': 'alpha', 'record': {'id': n}}
    state = lambda n: {'type': 'STATE', 'value': {'bookmarks': {
        'alpha': {'replication_key': 'id', 'replication_key_value': n}}}}
    source = make_cassette([schema, record(1), schema, record(2), schema])
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'once'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 2,
                 '--schema-once', '--state-every', 1, *(['--compact-states'] if compact else []))
    assert result.returncode == 0 and result.stdout == b'', result.stderr
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == [schema, record(1), state(1), record(2), state(2)]
        assert cassette.catalog == json.loads(before['catalog.json'])
        assert cassette.manifest['counts'] == {'alpha': 2}
        assert cassette.manifest['message_count'] == 5
        assert cassette.manifest['truncated'] is False
        assert cassette.manifest['trim'] == {
            'per_stream': 2, 'schema_once': True, 'state_every': 1,
            'source_sha256': json.loads(before['manifest.json'])['sha256'],
            **({'compact_states': True} if compact else {})}
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


@pytest.mark.parametrize('compact', [False, True])
@pytest.mark.parametrize('mode', [{}, {'final_state_only': True}, {'state_every': 1}, {'state_every': 3}])
def test_trim_schema_once_late_schema_and_adjacency(tmp_path, make_cassette, interleaved, compact, mode):
    first = interleaved[1]
    late = dict(first, stream='late')
    empty = {'type': 'STATE', 'value': {'bookmarks': {}}}
    record = {'type': 'RECORD', 'stream': 'events', 'record': {'id': 1},
              'time_extracted': '2024-01-01T00:00:00Z'}
    final = {'type': 'STATE', 'value': {'bookmarks': {
        'events': {'replication_key': 'id', 'replication_key_value': 1}}}}
    # Reverse object member order still compares identically; first keeps its position.
    repeat = dict(reversed(list(first.items())))
    source = make_cassette([empty, first, empty, repeat, empty, record, empty, first, empty, late, late])
    dest = tmp_path / 'late'
    trim(source, dest, per_stream=9, schema_once=True, compact_states=compact, **mode)
    if mode.get('final_state_only'):
        expected = [first, record, late, final]
    elif mode.get('state_every') == 1:
        expected = [first, record, final, late, final]  # Late *first* schema needs a final state.
    elif mode.get('state_every') == 3:
        expected = [first, record, late, final]
    elif compact:
        expected = [empty, first, empty, record, final, late, final]
    else:
        expected = [empty, first, empty, empty, record, final, final, late, final]
    with Cassette(dest) as cassette:
        assert list(cassette.messages()) == expected
        assert cassette.manifest['counts'] == {'events': 1, 'late': 0}
        assert cassette.manifest['message_count'] == len(expected)
        assert cassette.manifest['truncated'] is False
        assert cassette.catalog == json.loads((source / 'catalog.json').read_text())
    schemas = make_cassette([first, first, late, first, late])
    trim(schemas, tmp_path / 'schemas', per_stream=0, schema_once=True, compact_states=compact, **mode)
    with Cassette(tmp_path / 'schemas') as cassette:
        assert list(cassette.messages()) == [first, late, empty]
        assert cassette.manifest['truncated'] is False


@pytest.mark.parametrize('mode', [{}, {'compact_states': True}, {'final_state_only': True}, {'state_every': 2}])
@pytest.mark.parametrize('options,ids,streams', [
    ({'per_stream': 0}, [], ['events', 'audit.events']),
    ({'per_stream': 2}, [('events', 1), ('audit.events', 1), ('events', 2), ('audit.events', 2)], ['events', 'audit.events']),
    ({'per_stream': 0, 'stream_limits': {'audit.events': 2}, 'max_records': 1}, [('audit.events', 1)], ['events', 'audit.events']),
    ({'per_stream': 3, 'include_streams': ['audit.events']}, [('audit.events', n) for n in (1, 2, 3)], ['audit.events']),
    ({'per_stream': 3, 'exclude_streams': ['audit.events'], 'max_records': 1}, [('events', 1)], ['events']),
    ({'per_stream': 2, 'max_records': 3, 'drop_empty_streams': True}, [('events', 1), ('audit.events', 1), ('events', 2)], ['events', 'audit.events']),
    ({'per_stream': 2, 'stream_limits': {'events': 0}, 'drop_empty_streams': True}, [('audit.events', 1), ('audit.events', 2)], ['audit.events']),
])
def test_trim_schema_once_selection(tmp_path, make_cassette, interleaved, mode, options, ids, streams):
    schemas = interleaved[1:3]
    expanded = []
    for m in interleaved:
        expanded.append(m)
        if m['type'] == 'RECORD':
            expanded.append(schemas[0 if m['stream'] == 'events' else 1])
    expanded += schemas
    source = make_cassette(expanded)
    dest = tmp_path / 'selected'
    trim(source, dest, schema_once=True, **mode, **options)
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        assert [m for m in output if m['type'] == 'SCHEMA'] == [s for s in schemas if s['stream'] in streams]
        assert [(m['stream'], m['record']['id']) for m in rows(output)] == ids
        assert [e['stream'] for e in cassette.catalog['streams']] == streams
        assert cassette.manifest['counts'] == {s: sum(s == stream for stream, _ in ids) for s in streams}
        assert cassette.manifest['truncated'] is (len(ids) < 6)
        assert cassette.manifest['message_count'] == len(output)
        latest = {}
        for m in output:
            if m['type'] == 'RECORD':
                key = 'id' if m['stream'] == 'events' else 'at'
                latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
            elif m['type'] == 'STATE':
                assert m['value'] == {'bookmarks': latest}
        assert output[-1] == {'type': 'STATE', 'value': {'bookmarks': latest}}


@pytest.mark.parametrize('mode', [{}, {'compact_states': True}, {'final_state_only': True}, {'state_every': 3}])
@pytest.mark.parametrize('cut', range(5))
def test_trim_schema_once_keyed_resume(tmp_path, make_cassette, interleaved, mode, cut):
    expanded = interleaved[:3]
    for m in interleaved[3:]:
        expanded.append(m)
        if m['type'] == 'RECORD':
            expanded.append(interleaved[1 if m['stream'] == 'events' else 2])
    source = make_cassette(expanded)
    dest = tmp_path / 'resumable'
    trim(source, dest, per_stream=2, schema_once=True, drop_empty_streams=True, **mode)
    expected = rows(interleaved)[:4]
    result, before = replay_cli(tmp_path, dest, fail_after_records=cut, fail_exit_code=19,
                                time_shift='2030-01-01T00:00:00Z', state_message_frequency=0)
    assert result.returncode == 19 and len(rows(before)) == cut, result.stderr
    latest = {}
    for m in expected[:cut]:
        key = 'id' if m['stream'] == 'events' else 'at'
        latest[m['stream']] = {'replication_key': key, 'replication_key_value': m['record'][key]}
    assert before[-1] == {'type': 'STATE', 'value': {'bookmarks': latest}}
    result, after = replay_cli(tmp_path, dest, state=before[-1]['value'],
                              time_shift='2040-01-01T00:00:00Z', state_message_frequency=1)
    assert result.returncode == 0, result.stderr
    assert [(m['stream'], m['record']['id']) for m in rows(before) + rows(after)] == [
        (m['stream'], m['record']['id']) for m in expected]
    assert all(m['record']['at'] != expected[1]['record']['at'] for m in rows(before) + rows(after)
               if m['stream'] == 'audit.events')
    result, final = replay_cli(tmp_path, dest, state=after[-1]['value'])
    assert result.returncode == 0 and rows(final) == [], result.stderr


@pytest.mark.parametrize('mode', [{}, {'compact_states': True}, {'final_state_only': True}, {'state_every': 1}])
def test_trim_schema_once_compatibility_determinism_full_table(tmp_path, make_cassette, messages, mode):
    expanded = messages + [messages[0], messages[1]]
    source = make_cassette(expanded, rules=scrub_rules(['customers.email=hash']), salt=b'T' * 32)
    artifacts = lambda p: {f.name: f.read_bytes() for f in p.iterdir()}
    before = artifacts(source)
    a, b, c, d = [tmp_path / n for n in 'abcd']
    trim(source, a, per_stream=9, **mode)
    trim(source, b, per_stream=9, schema_once=False, **mode)
    assert artifacts(a) == artifacts(b)
    assert 'schema_once' not in json.loads((a / 'manifest.json').read_text())['trim']
    trim(source, c, per_stream=9, schema_once=True, **mode)
    trim(source, d, schema_once=True, per_stream=9, **mode)
    assert artifacts(c) == artifacts(d)
    with Cassette(c) as cassette:
        output = list(cassette.messages())
        assert len([m for m in output if m['type'] == 'SCHEMA']) == 2
        assert cassette.manifest['truncated'] is False
        assert cassette.manifest['scrub'] == json.loads(before['manifest.json'])['scrub']
        assert cassette.manifest['recorded_at'] == json.loads(before['manifest.json'])['recorded_at']
        assert cassette.manifest['message_count'] == len(output)
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((c / name).read_bytes()).hexdigest() == digest
    result, replayed = replay_cli(tmp_path, c, state=output[-1]['value'])
    assert result.returncode == 0, result.stderr
    assert rows(replayed) == [m for m in messages if m['type'] == 'RECORD' and m['stream'] == 'products']
    truncated = make_cassette(expanded, limit=2)
    trim(truncated, tmp_path / 'inherited', per_stream=9, schema_once=True, **mode)
    with Cassette(tmp_path / 'inherited') as cassette:
        assert cassette.manifest['truncated'] is True
    assert artifacts(source) == before


@pytest.mark.parametrize('bad', [None, 0, 1, 'true', [], {}])
def test_trim_schema_once_strict_boolean(tmp_path, make_cassette, interleaved, bad):
    source = make_cassette(interleaved)
    dest = tmp_path / 'bad'
    with pytest.raises(ReplayError, match='schema-once must be boolean'):
        trim(source, dest, per_stream=1, schema_once=bad)
    assert not dest.exists()


@pytest.mark.parametrize('flags', [['--schema-once=true'], ['--schema-once', 'false'],
                                 ['--schema-once', '1']])
def test_trim_schema_once_invalid_cli(tmp_path, make_cassette, interleaved, flags):
    source = make_cassette(interleaved)
    dest = tmp_path / 'bad'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 1, *flags)
    assert result.returncode == 2 and result.stdout == b'' and not dest.exists()


@pytest.mark.parametrize('corruption,error', [('schema', b'schema evolution'),
    ('excluded-record', b'schema type'), ('discarded-record', b'schema type'),
    ('state', b'RFC3339'), ('checksum', b'checksum mismatch')])
def test_trim_schema_once_full_preflight(tmp_path, make_cassette, interleaved, corruption, error):
    messages = copy.deepcopy(interleaved) + [copy.deepcopy(interleaved[2])]
    source = make_cassette(messages)
    if corruption == 'schema':
        messages[-1]['schema']['title'] = 'changed schema'
    elif corruption.endswith('record'):
        messages[-5 if corruption == 'excluded-record' else -3]['record']['id'] = 'bad'
    elif corruption == 'state':
        messages[-2]['value']['bookmarks']['audit.events']['replication_key_value'] = '2024-01-03T00:00:00+00:60'
    data = gzip.compress(('\n'.join(json.dumps(m) for m in messages) + '\n').encode())
    (source / 'messages.jsonl.gz').write_bytes(data)
    if corruption != 'checksum':
        manifest = json.loads((source / 'manifest.json').read_text())
        manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
        (source / 'manifest.json').write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / 'bad'
    result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0,
                 '--exclude-stream', 'events', '--schema-once', '--state-every', 1)
    assert result.returncode == 2 and result.stdout == b'', result.stderr
    assert error in result.stderr and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_trim_schema_once_guards(tmp_path, make_cassette, interleaved):
    source = make_cassette(interleaved)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'sentinel').write_bytes(b'keep')
    for dest in (source, alias, source / 'child', existing):
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0, '--schema-once')
        assert result.returncode == 2 and result.stdout == b''
    assert not (source / 'child').exists()
    assert (existing / 'sentinel').read_bytes() == b'keep'
    for flags in (['--drop-empty-streams'], ['--exclude-stream', 'unknown'],
                  ['--state-every', '0'], ['--final-state-only', '--state-every', '1'],
                  ['--stream-limit', 'events=-1'], ['--max-records', '-1']):
        dest = tmp_path / 'bad'
        result = cli('singer-replay', 'trim', source, '-o', dest, '--per-stream', 0, '--schema-once', *flags)
        assert result.returncode == 2 and result.stdout == b'' and not dest.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
