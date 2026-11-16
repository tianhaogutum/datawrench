"""Read-only trim plans must describe the exact streaming writer selection."""
import copy
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from datawrench.singer_replay.cassette import Cassette, trim
from datawrench.singer_replay.common import ReplayError, encode


def cli(source, *args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'singer-replay',
                           'trim', str(source), *map(str, args)], capture_output=True, timeout=15)


def schema(stream, *, keyed=True):
    return {'type': 'SCHEMA', 'stream': stream, 'key_properties': ['id'],
            **({'replication_key': 'id'} if keyed else {}),
            'schema': {'type': 'object', 'properties': {'id': {'type': 'integer'}},
                       'required': ['id']}}


def record(stream, n):
    return {'type': 'RECORD', 'stream': stream, 'record': {'id': n}}


def snapshot(path):
    return {str(p.relative_to(path)): p.read_bytes() if p.is_file() else None
            for p in path.rglob('*')}


def test_dry_run_cli_exact_summary_no_writes(tmp_path, make_cassette):
    s = schema('alpha')
    source = make_cassette([s, record('alpha', 1), s, record('alpha', 2), s])
    before = snapshot(tmp_path)
    expected = b'{"counts":{"alpha":2},"message_count":5,"truncated":false,"streams":["alpha"]}\n'
    for _ in range(2):
        result = cli(source, '--dry-run', '--per-stream', 2, '--schema-once', '--state-every', 1)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected and result.stderr == b''
        assert snapshot(tmp_path) == before
    assert trim(source, per_stream=2, dry_run=True, schema_once=True, state_every=1) == json.loads(expected)


@pytest.fixture
def planning_source(make_cassette):
    # Source order differs from alphabetical order; full-table records count toward cadence.
    a, b, empty = schema('zeta'), schema('alpha', keyed=False), schema('empty')
    state = {'type': 'STATE', 'value': {'bookmarks': {}}}
    return make_cassette([state, a, b, record('zeta', 1), state, state,
                          record('alpha', 1), a, record('zeta', 2), state,
                          record('alpha', 2), record('zeta', 3), state, a, empty])


@pytest.mark.parametrize('selection', [
    {}, {'per_stream': 0}, {'max_records': 0}, {'max_records': 1},
    {'stream_limits': {'zeta': 0, 'alpha': 4}},
    {'include_streams': ['alpha', 'zeta'], 'stream_limits': {'alpha': 1}},
    {'exclude_streams': ['zeta']}, {'max_records': 3, 'drop_empty_streams': True},
])
@pytest.mark.parametrize('checkpoints', [{}, {'final_state_only': True}, {'state_every': 1}, {'state_every': 3}])
@pytest.mark.parametrize('schema_once,compact_states', [(False, False), (True, False), (True, True)])
def test_dry_run_matches_real_trim(tmp_path, planning_source, selection, checkpoints,
                                  schema_once, compact_states):
    kwargs = {'per_stream': 2, **selection, **checkpoints,
              'schema_once': schema_once, 'compact_states': compact_states}
    before = snapshot(tmp_path)
    plan = trim(planning_source, dry_run=True, **kwargs)
    assert snapshot(tmp_path) == before
    dest = tmp_path / 'actual'
    manifest = trim(planning_source, dest, **kwargs)
    with Cassette(dest) as cassette:
        output = list(cassette.messages())
        streams = [s['tap_stream_id'] for s in cassette.catalog['streams']]
        assert plan == {'counts': manifest['counts'], 'message_count': len(output),
                        'truncated': manifest['truncated'], 'streams': streams}
    assert 'dry_run' not in manifest['trim']
    assert trim(planning_source, dry_run=True, **kwargs) == plan


def test_dry_run_schema_only_and_inherited_truncation(tmp_path, make_cassette):
    source = make_cassette([schema('alpha'), schema('alpha')])
    assert trim(source, per_stream=0, dry_run=True, schema_once=True, state_every=1) == {
        'counts': {'alpha': 0}, 'message_count': 2, 'truncated': False, 'streams': ['alpha']}
    source = make_cassette([schema('alpha'), record('alpha', 1), record('alpha', 2)], limit=1)
    assert trim(source, per_stream=99, dry_run=True)['truncated'] is True


@pytest.mark.parametrize('args', [
    ['--dry-run', '-o', 'unused'], ['--dry-run', '-o', ''], [],
    ['--dry-run', '--per-stream', '-1'], ['--dry-run', '--max-records', '-1'],
    ['--dry-run', '--stream-limit', 'alpha=1', '--stream-limit', 'alpha=1'],
    ['--dry-run', '--stream-limit', 'alpha=bad'], ['--dry-run', '--stream-limit', 'unknown=1'],
    ['--dry-run', '--include-stream', 'unknown'], ['--dry-run', '--exclude-stream', 'unknown'],
    ['--dry-run', '--include-stream', 'alpha', '--include-stream', 'alpha'],
    ['--dry-run', '--include-stream', 'alpha', '--exclude-stream', 'empty'],
    ['--dry-run', '--exclude-stream', 'alpha', '--exclude-stream', 'zeta', '--exclude-stream', 'empty'],
    ['--dry-run', '--include-stream', 'alpha', '--stream-limit', 'zeta=0'],
    ['--dry-run', '--state-every', 0], ['--dry-run', '--state-every', 1, '--final-state-only'],
    ['--dry-run', '--per-stream', 0, '--drop-empty-streams'],
])
def test_dry_run_cli_rejects_without_writes(tmp_path, planning_source, args):
    before = snapshot(tmp_path)
    result = cli(planning_source, '--per-stream', 2, *args)
    assert result.returncode == 2 and result.stdout == b'', result
    assert snapshot(tmp_path) == before
    assert not Path('unused').exists()


@pytest.mark.parametrize('kwargs', [
    {'dry_run': None}, {'dry_run': 1}, {'dry_run': 'true'},
    {'dry_run': True, 'directory': ''}, {'dry_run': True, 'directory': 'unused'},
    {'dry_run': False}, {'dry_run': True, 'state_every': True},
    {'dry_run': True, 'schema_once': 1}, {'dry_run': True, 'per_stream': True},
])
def test_dry_run_api_validation(tmp_path, planning_source, kwargs):
    before = snapshot(tmp_path)
    with pytest.raises(ReplayError):
        trim(planning_source, **{'per_stream': 2, **kwargs})
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('corruption', ['excluded-record', 'discarded-record', 'removed-state',
                                       'evolved-schema', 'checksum', 'catalog', 'counts', 'gzip'])
def test_dry_run_full_source_validation(tmp_path, make_cassette, corruption):
    a, b = schema('alpha'), schema('beta')
    messages = [a, b, record('alpha', 1), record('beta', 1), record('alpha', 2),
                {'type': 'STATE', 'value': {'bookmarks': {}}}, a]
    source = make_cassette(messages)
    manifest = json.loads((source / 'manifest.json').read_text())
    if corruption == 'excluded-record':
        messages[3]['record']['id'] = 'invalid'
    elif corruption == 'discarded-record':
        messages[4]['record']['id'] = 'invalid'
    elif corruption == 'removed-state':
        messages[5]['value'] = {'opaque': 'invalid'}
    elif corruption == 'evolved-schema':
        messages[-1] = copy.deepcopy(a)
        messages[-1]['schema']['description'] = 'evolved'
    elif corruption == 'catalog':
        (source / 'catalog.json').write_text('{"streams":[]}')
        manifest['sha256']['catalog.json'] = hashlib.sha256((source / 'catalog.json').read_bytes()).hexdigest()
    elif corruption == 'counts':
        manifest['counts']['alpha'] = 999
    data = gzip.compress(('\n'.join(encode(m) for m in messages) + '\n').encode(), mtime=0)
    if corruption == 'gzip':
        data = data[:-5]
    (source / 'messages.jsonl.gz').write_bytes(data)
    if corruption != 'checksum':
        manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
    (source / 'manifest.json').write_text(encode(manifest))
    before = snapshot(tmp_path)
    result = cli(source, '--dry-run', '--per-stream', 1, '--include-stream', 'alpha',
                 '--schema-once', '--final-state-only')
    assert result.returncode == 2 and result.stdout == b'', result
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('mode', [{}, {'final_state_only': True}, {'state_every': 1}, {'state_every': 3}])
@pytest.mark.parametrize('bound', ['MAX_LINE', 'MAX_MESSAGES_BYTES'])
def test_dry_run_output_bounds_no_writes(tmp_path, make_cassette, monkeypatch, mode, bound, capsys):
    from datawrench.singer_replay.cli import record_main
    messages = []
    for stream in ['left', 'right']:
        s = schema(stream)
        s['schema']['properties']['id'] = {'type': 'string'}
        messages += [s, {'type': 'RECORD', 'stream': stream, 'record': {'id': 'x' * 1000}},
                     {'type': 'STATE', 'value': {'bookmarks': {}}}]
    source = make_cassette(messages)
    sizes = [len((encode(m) + '\n').encode()) for m in messages]
    monkeypatch.setattr('datawrench.singer_replay.cassette.' + bound,
                        max(sizes) if bound == 'MAX_LINE' else sum(sizes))
    before = snapshot(tmp_path)
    with pytest.raises(ReplayError, match='output message'):
        trim(source, per_stream=2, dry_run=True, **mode)
    args = [str(source), '--per-stream', '2', '--dry-run']
    for key, value in mode.items():
        args += ['--' + key.replace('_', '-')]
        if value is not True:
            args += [str(value)]
    assert record_main(['trim', *args]) == 2
    assert capsys.readouterr().out == ''
    assert snapshot(tmp_path) == before


def test_dry_run_metadata_bounds(tmp_path, planning_source, monkeypatch):
    # Source input limits remain intact. This smaller output bound affects catalog/summary only.
    monkeypatch.setattr('datawrench.singer_replay.cassette.MAX_JSON', 400)
    # Cassette preflight also uses MAX_JSON: allow it first, then lower only for planning.
    original = Cassette.__enter__
    def enter(cassette):
        monkeypatch.setattr('datawrench.singer_replay.cassette.MAX_JSON', 16 * 1024 * 1024)
        result = original(cassette)
        monkeypatch.setattr('datawrench.singer_replay.cassette.MAX_JSON', 400)
        return result
    monkeypatch.setattr(Cassette, '__enter__', enter)
    before = snapshot(tmp_path)
    with pytest.raises(ReplayError, match='metadata'):
        trim(planning_source, per_stream=2, dry_run=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('options,count', [
    ({}, 8), ({'schema_once': True}, 6),
    ({'state_every': 1}, 9), ({'state_every': 1, 'schema_once': True}, 7),
    ({'final_state_only': True}, 7), ({'final_state_only': True, 'schema_once': True}, 5),
])
def test_dry_run_late_schema_final_checkpoint_counts(make_cassette, options, count):
    a = schema('alpha')
    source = make_cassette([{'type': 'STATE', 'value': {'bookmarks': {}}}, a,
                            record('alpha', 1), a, record('alpha', 2), a, schema('late')])
    assert trim(source, per_stream=2, dry_run=True, **options) == {
        'counts': {'alpha': 2, 'late': 0}, 'message_count': count,
        'truncated': False, 'streams': ['alpha', 'late']}


@pytest.mark.parametrize('once,compact,count', [(False, False, 7), (False, True, 5),
                                               (True, False, 6), (True, True, 3)])
def test_dry_run_compaction_after_schema_suppression(make_cassette, once, compact, count):
    a = schema('alpha')
    state = {'type': 'STATE', 'value': {'bookmarks': {}}}
    source = make_cassette([a, record('alpha', 1), state, state, a,
                            record('alpha', 2), state, state])
    assert trim(source, per_stream=1, dry_run=True, schema_once=once,
                compact_states=compact) == {
        'counts': {'alpha': 1}, 'message_count': count,
        'truncated': True, 'streams': ['alpha']}
