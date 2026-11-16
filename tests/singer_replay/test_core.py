import copy
import gzip
import hashlib
import io
import json
import os
from datetime import timedelta

import pytest

from datawrench.singer_replay.cassette import Cassette, record, scrub_rules
from datawrench.singer_replay.cli import config_from
from datawrench.singer_replay.common import MAX_LINE, ReplayError, decode, timestamp
from datawrench.singer_replay.replay import InjectedFailure, replay


def run(path, **kwargs):
    output = []
    config = config_from({'cassette_path': str(path), **kwargs.pop('config', {})})
    with Cassette(path) as cassette:
        replay(cassette, config, output.append, **kwargs)
    return output


def records(output):
    return [m for m in output if m['type'] == 'RECORD']


def test_round_trip_is_equal(messages, make_cassette):
    path = make_cassette(messages)
    assert run(path) == messages
    manifest = json.loads((path / 'manifest.json').read_text())
    assert manifest['counts'] == {'customers': 3, 'products': 1}
    assert manifest['message_count'] == 8
    assert manifest['complete'] is True
    for name, digest in manifest['sha256'].items():
        assert hashlib.sha256((path / name).read_bytes()).hexdigest() == digest
    assert os.stat(path).st_mode & 0o777 == 0o700
    assert all(os.stat(p).st_mode & 0o777 == 0o600 for p in path.iterdir())


def test_discovery_mapping(messages, make_cassette):
    with Cassette(make_cassette(messages)) as cassette:
        streams = cassette.catalog['streams']
        assert [s['tap_stream_id'] for s in streams] == ['customers', 'products']
        c = streams[0]
        assert c['key_properties'] == ['id']
        assert c['replication_key'] == 'updated_at'
        assert c['metadata'][0]['metadata']['replication-method'] == 'INCREMENTAL'
        assert c['metadata'][1]['metadata']['inclusion'] == 'automatic'
        assert c['schema'] == messages[0]['schema']
        assert streams[1]['metadata'][0]['metadata']['replication-method'] == 'FULL_TABLE'


def test_selection_nested_arrays_and_automatic_keys(messages, make_cassette, capsys):
    path = make_cassette(messages)
    metadata = [{'breadcrumb': [], 'metadata': {'selected': True}}]
    for field in ['email', 'id', 'updated_at']:
        metadata.append({'breadcrumb': ['properties', field], 'metadata': {'selected': False}})
    metadata.extend([
        {'breadcrumb': ['properties', 'profile', 'properties', 'phone'], 'metadata': {'selected': False}},
        {'breadcrumb': ['properties', 'tags', 'items', 'properties', 'internal'], 'metadata': {'selected': False}}])
    output = run(path, catalog={'streams': [{'tap_stream_id': 'customers', 'metadata': metadata},
                                           {'tap_stream_id': 'unknown'}]})
    assert len(records(output)) == 3
    assert all(m.get('stream', 'customers') == 'customers' for m in output)
    assert 'unknown catalog stream' in capsys.readouterr().err
    schema = output[0]['schema']
    assert 'email' not in schema['properties'] and 'email' not in schema['required']
    assert 'phone' not in schema['properties']['profile']['required']
    for msg in records(output):
        assert set(msg['record']) == {'id', 'updated_at', 'profile', 'tags'}
        assert msg['record']['profile'] == {'city': 'Demo City'}
        assert msg['record']['tags'] == [{'label': 'demo'}]
    assert list(output[-1]['value']['bookmarks']) == ['customers']


def test_explicit_false_parent_beats_child_and_empty_selection(messages, make_cassette):
    path = make_cassette(messages)
    cat = {'streams': [{'tap_stream_id': 'customers', 'metadata': [
        {'breadcrumb': [], 'metadata': {'selected': False}},
        {'breadcrumb': ['properties', 'email'], 'metadata': {'selected': True}}]}]}
    assert all(m['type'] == 'STATE' for m in run(path, catalog=cat))
    assert records(run(path, catalog={'streams': []})) == []


def test_state_resume_and_final_state(messages, make_cassette):
    path = make_cassette(messages)
    state = messages[4]['value']
    output = run(path, state=state, config={'state_message_frequency': 1})
    assert [m['record']['id'] for m in records(output) if m['stream'] == 'customers'] == [2, 3]
    assert len([m for m in output if m['type'] == 'STATE']) >= 3
    assert output[-1] == messages[-1]
    # Supplying the final state never regresses it, even at older source STATE boundaries.
    output = run(path, state=messages[-1]['value'])
    assert [m['stream'] for m in records(output)] == ['products']
    assert all(m['value'] == messages[-1]['value'] for m in output if m['type'] == 'STATE')


def test_state_unkeyed_stream_replays_with_notice(messages, make_cassette, capsys):
    state = {'bookmarks': {'products': {'replication_key': 'sku', 'replication_key_value': 'ZZZ'}}}
    output = run(make_cassette(messages), state=state)
    assert any(m['stream'] == 'products' for m in records(output))
    assert 'no replication key; full replay' in capsys.readouterr().err
    assert 'products' not in output[-1]['value']['bookmarks']


def test_numeric_state_resume_and_zero(messages, make_cassette):
    msgs = [copy.deepcopy(m) for m in messages if m.get('stream') == 'customers']
    msgs[0]['bookmark_properties'] = ['id']
    path = make_cassette(msgs)
    output = run(path, state={'bookmarks': {'customers': {'replication_key': 'id', 'replication_key_value': 2}}})
    assert [m['record']['id'] for m in records(output)] == [3]
    assert output[-1]['value']['bookmarks']['customers']['replication_key_value'] == 3


def test_scrub_hash_mask_drop_and_null(messages, make_cassette):
    for m in messages:
        if m.get('stream') == 'products' and m['type'] == 'RECORD':
            m['record']['available'] = False
    rules = scrub_rules(['customers.email=hash', 'customers.profile=drop', 'products.price=mask'])
    one = make_cassette(messages, rules=rules, salt=b'A' * 32)
    two = make_cassette(messages, rules=rules, salt=b'A' * 32)
    other = make_cassette(messages, rules=rules, salt=b'B' * 32)
    assert (one / 'messages.jsonl.gz').read_bytes() == (two / 'messages.jsonl.gz').read_bytes()
    assert (one / 'messages.jsonl.gz').read_bytes() != (other / 'messages.jsonl.gz').read_bytes()
    for file in one.iterdir():
        data = gzip.decompress(file.read_bytes()) if file.suffix == '.gz' else file.read_bytes()
        assert b'alice@example.invalid' not in data and b'bob@example.invalid' not in data
        assert b'555-0100' not in data and b'example-only@' not in data
    out = run(one)
    cs = [m['record'] for m in records(out) if m['stream'] == 'customers']
    assert cs[0]['email'] == cs[2]['email'] and len(cs[0]['email']) == 64
    assert 'profile' not in cs[0]
    assert [m['record']['price'] for m in records(out) if m['stream'] == 'products'] == ['***']
    messages[2]['record']['email'] = None
    assert records(run(make_cassette(messages, rules=rules)))[0]['record']['email'] is None


@pytest.mark.parametrize('rule', ['customers.id=drop', 'customers.updated_at=hash',
                                  'customers.missing=mask', 'missing.email=hash'])
def test_bad_scrub_fails_closed(messages, tmp_path, rule):
    path = tmp_path / 'bad'
    with pytest.raises(ReplayError):
        record(io.BytesIO(('\n'.join(json.dumps(m) for m in messages) + '\n').encode()),
               path, rules=scrub_rules([rule]))
    assert json.loads((path / 'manifest.json').read_text())['complete'] is False
    with pytest.raises(ReplayError, match='incomplete'):
        with Cassette(path):
            pass


@pytest.mark.parametrize('rule', ['bad', 'a.b=invalid', '.f=hash', 'a.=drop'])
def test_scrub_syntax(rule):
    with pytest.raises(ReplayError):
        scrub_rules([rule])


def test_time_shift_nested_and_source_time_state(messages, make_cassette):
    path = make_cassette(messages)
    out = run(path, config={'time_shift': '2030-02-03T12:00:00Z'})
    shifted = [m for m in records(out) if m['stream'] == 'customers']
    assert timestamp(shifted[-1]['record']['updated_at']) == timestamp('2030-02-03T12:00:00Z')
    assert timestamp(shifted[-1]['time_extracted']) == timestamp('2030-02-03T12:00:01Z')
    assert timestamp(shifted[1]['record']['updated_at']) - timestamp(shifted[0]['record']['updated_at']) == timedelta(days=1)
    assert out[-1] == messages[-1]  # bookmarks remain in original cassette time
    assert not any(m['stream'] == 'customers' for m in records(run(path, state=out[-1]['value'], config={'time_shift': 'now'})))


def test_time_shift_requires_timestamp(messages, make_cassette):
    out = [m for m in messages if m.get('stream') == 'products']
    with pytest.raises(ReplayError, match='requires'):
        run(make_cassette(out), config={'time_shift': 'now'})


def test_fault_checkpoint_resumes_without_losing_records(messages, make_cassette):
    path = make_cassette(messages)
    output = []
    with Cassette(path) as cassette, pytest.raises(InjectedFailure) as exc:
        replay(cassette, config_from({'cassette_path': str(path), 'fail_after_records': 3,
                                     'fail_exit_code': 17}), output.append)
    assert exc.value.code == 17 and len(records(output)) == 3
    assert output[-1]['type'] == 'STATE'
    remaining = run(path, state=output[-1]['value'])
    assert [m['record']['id'] for m in records(remaining) if m['stream'] == 'customers'] == [3]


@pytest.mark.parametrize('changes', [
    {'fail_exit_code': 0}, {'fail_exit_code': 126}, {'fail_exit_code': True},
    {'fail_after_records': -1}, {'delay_ms': 60001}, {'state_message_frequency': 1.5},
    {'time_shift': 123}, {'time_shift': 'yesterday'}, {'unknown': 2}, {'cassette_path': ''}])
def test_config_boundaries(changes):
    with pytest.raises(ReplayError):
        config_from({'cassette_path': '.', **changes})


@pytest.mark.parametrize('member', ['messages.jsonl.gz', 'catalog.json'])
def test_checksum_corruption(messages, make_cassette, member):
    path = make_cassette(messages)
    with (path / member).open('ab') as dst:
        dst.write(b'corruption')
    with pytest.raises(ReplayError, match='checksum'):
        run(path)


def test_rechecks_schema_even_if_checksums_rewritten(messages, make_cassette):
    path = make_cassette(messages)
    messages[2]['record']['id'] = 'bad'
    (path / 'messages.jsonl.gz').write_bytes(gzip.compress(('\n'.join(json.dumps(x) for x in messages) + '\n').encode()))
    manifest = json.loads((path / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256((path / 'messages.jsonl.gz').read_bytes()).hexdigest()
    (path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ReplayError, match='schema type'):
        run(path)


def test_member_symlinks_and_missing(messages, make_cassette, tmp_path):
    path = make_cassette(messages)
    original = path / 'messages.jsonl.gz'
    moved = tmp_path / 'original.gz'
    original.rename(moved)
    original.symlink_to(moved)
    with pytest.raises(OSError):
        run(path)
    original.unlink()
    with pytest.raises(OSError):
        run(path)


def test_record_no_overwrite(messages, make_cassette):
    path = make_cassette(messages)
    before = (path / 'manifest.json').read_bytes()
    with pytest.raises(FileExistsError):
        record(io.BytesIO(b''), path)
    assert (path / 'manifest.json').read_bytes() == before


def test_limit_and_empty_cassette(messages, make_cassette, tmp_path):
    path = make_cassette(messages, limit=1)
    assert len(records(run(path))) == 1
    assert json.loads((path / 'manifest.json').read_text())['truncated'] is True
    with pytest.raises(ReplayError, match='no SCHEMA'):
        record(io.BytesIO(b''), tmp_path / 'empty')


@pytest.mark.parametrize('bad', [b'{', b'{"type":"STATE","type":"RECORD"}\n',
                                 b'{"type":"LOG"}\n', b'\xff\n', b'\n',
                                 b'{"type":"STATE","value":{"token":"secret"}}\n'])
def test_invalid_messages_mark_incomplete(tmp_path, bad):
    path = tmp_path / 'bad'
    with pytest.raises(ReplayError):
        record(io.BytesIO(bad), path)
    assert json.loads((path / 'manifest.json').read_text())['complete'] is False


@pytest.mark.parametrize('mutate', [
    lambda m: m[2]['record'].update({'extra': 1}),
    lambda m: m[2]['record'].pop('id'),
    lambda m: m[2]['record'].update({'updated_at': 'naive'}),
    lambda m: m[0].update({'bookmark_properties': ['id', 'updated_at']}),
    lambda m: m[0]['schema'].update({'$ref': 'https://invalid.example/schema'}),
    lambda m: m[0].update({'stream': '../../escaped'}),
    lambda m: m[5]['record'].update({'updated_at': '2024-01-01T12:00:00Z'}),
    lambda m: m[4]['value']['bookmarks']['customers'].update({'replication_key_value': '9999-01-01T00:00:00Z'}),
])
def test_protocol_boundaries(messages, tmp_path, mutate):
    mutate(messages)
    with pytest.raises(ReplayError):
        record(io.BytesIO(('\n'.join(json.dumps(x) for x in messages) + '\n').encode()), tmp_path / 'bad')
    assert json.loads((tmp_path / 'bad' / 'manifest.json').read_text())['complete'] is False


def test_schema_evolution_and_record_before_schema(messages, tmp_path):
    changed = copy.deepcopy(messages[0])
    changed['schema']['title'] = 'Changed'
    for i, msgs in enumerate([[messages[2]], [messages[0], changed]]):
        with pytest.raises(ReplayError):
            record(io.BytesIO(('\n'.join(json.dumps(x) for x in msgs) + '\n').encode()), tmp_path / str(i))


def test_message_size_bound(tmp_path):
    with pytest.raises(ReplayError, match='exceeds 16 MiB'):
        record(io.BytesIO(b'x' * (MAX_LINE + 1)), tmp_path / 'big')


def test_json_finite_numbers_and_duplicates():
    for value in [b'{"n":NaN}', b'{"n":1,"n":2}']:
        with pytest.raises(ReplayError):
            decode(value)


@pytest.mark.parametrize('offset', ['+00:60', '-00:60', '+01:99', '-23:99', '+24:00'])
def test_timestamp_rejects_invalid_offsets(offset):
    with pytest.raises(ReplayError, match='RFC3339'):
        timestamp('2024-01-01T12:00:00' + offset)


@pytest.mark.parametrize('offset, utc', [
    ('+00:00', '2024-01-01T12:00:00Z'),
    ('-00:00', '2024-01-01T12:00:00Z'),
    ('+05:30', '2024-01-01T06:30:00Z'),
    ('-03:45', '2024-01-01T15:45:00Z'),
    ('+23:59', '2023-12-31T12:01:00Z'),
    ('-23:59', '2024-01-02T11:59:00Z'),
])
def test_timestamp_valid_offset_boundaries(offset, utc):
    assert timestamp('2024-01-01T12:00:00' + offset) == timestamp(utc)


@pytest.mark.parametrize('entry', [
    {'tap_stream_id': 'customers', 'metadata': [
        {'breadcrumb': ['properties', 'unknown'], 'metadata': {'selected': False}}]},
    {'tap_stream_id': 'customers', 'metadata': [
        {'breadcrumb': [], 'metadata': {'selected': 'false'}}]},
    {'tap_stream_id': 'customers', 'schema': {'type': 'object', 'properties': {}}},
    {'tap_stream_id': 'customers', 'replication_key': 'id'},
    {'tap_stream_id': 'customers', 'metadata': [
        {'breadcrumb': ['properties', 'tags', 'items'], 'metadata': {'selected': False}}]},
])
def test_invalid_catalog_emits_nothing(messages, make_cassette, entry):
    output = []
    path = make_cassette(messages)
    with Cassette(path) as cassette, pytest.raises(ReplayError):
        replay(cassette, config_from({'cassette_path': str(path)}), output.append,
               catalog={'streams': [entry]})
    assert not output


def test_bad_state_emits_nothing(messages, make_cassette):
    path = make_cassette(messages)
    output = []
    state = {'bookmarks': {'customers': {'replication_key': 'id', 'replication_key_value': 10}}}
    with Cassette(path) as cassette, pytest.raises(ReplayError):
        replay(cassette, config_from({'cassette_path': str(path)}), output.append, state=state)
    assert not output


def test_nested_timestamp_shift(make_cassette):
    schema = {'type': 'object', 'properties': {
        'id': {'type': 'integer'}, 'dates': {'type': 'array', 'items': {
            'type': 'object', 'properties': {'at': {'type': ['null', 'string'], 'format': 'date-time'}}}}}}
    msgs = [{'type': 'SCHEMA', 'stream': 'events', 'key_properties': ['id'], 'schema': schema},
            {'type': 'RECORD', 'stream': 'events', 'record': {'id': 1, 'dates': [
                {'at': '2024-01-01T00:00:00+02:00'}, {'at': None}, {'at': '2024-01-03T00:00:00Z'}]}}]
    out = run(make_cassette(msgs), config={'time_shift': '2030-01-03T00:00:00Z'})
    dates = records(out)[0]['record']['dates']
    assert timestamp(dates[0]['at']) == timestamp('2029-12-31T22:00:00Z')
    assert dates[1]['at'] is None
    assert timestamp(dates[2]['at']) == timestamp('2030-01-03T00:00:00Z')


def test_time_shift_overflow_is_preflighted(messages, make_cassette):
    path = make_cassette(messages)
    output = []
    with Cassette(path) as cassette, pytest.raises(OverflowError):
        replay(cassette, config_from({'cassette_path': str(path),
                                     'time_shift': '9999-12-31T23:59:59Z'}), output.append)
    assert not output


def test_delay_applies_only_to_emitted_records(messages, make_cassette, monkeypatch):
    waits = []
    monkeypatch.setattr('datawrench.singer_replay.replay.time.sleep', waits.append)
    path = make_cassette(messages)
    output = run(path, state=messages[-1]['value'], config={'delay_ms': 2})
    assert len(records(output)) == 1 and waits == [0.002]


def test_gzip_truncation_with_valid_checksum(messages, make_cassette):
    path = make_cassette(messages)
    file = path / 'messages.jsonl.gz'
    file.write_bytes(file.read_bytes()[:-7])
    manifest = json.loads((path / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(file.read_bytes()).hexdigest()
    (path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(EOFError):
        run(path)


def test_malformed_structure_never_marks_complete(tmp_path):
    data = b'{"type":"SCHEMA","stream":"x","key_properties":[],"schema":{"type":[[]]}}\n'
    with pytest.raises(ReplayError):
        record(io.BytesIO(data), tmp_path / 'bad-structure')
    assert json.loads((tmp_path / 'bad-structure' / 'manifest.json').read_text())['complete'] is False


def test_catalog_cannot_silently_change_replication_method(messages, make_cassette):
    cat = {'streams': [{'tap_stream_id': 'customers', 'metadata': [
        {'breadcrumb': [], 'metadata': {'replication-method': 'FULL_TABLE', 'selected': True}}]}]}
    with pytest.raises(ReplayError, match='replication or inclusion'):
        run(make_cassette(messages), catalog=cat)


def test_hash_object_fields_are_independent_of_object_key_order(messages, make_cassette):
    rules = scrub_rules(['customers.profile=hash'])
    first = run(make_cassette(messages, rules=rules, salt=b'K' * 32))
    messages[2]['record']['profile'] = dict(reversed(list(messages[2]['record']['profile'].items())))
    second = run(make_cassette(messages, rules=rules, salt=b'K' * 32))
    assert records(first)[0]['record']['profile'] == records(second)[0]['record']['profile']


def test_metadata_limit_leaves_an_incomplete_cassette(messages, tmp_path, monkeypatch):
    monkeypatch.setattr('datawrench.singer_replay.cassette.MAX_JSON', 512)
    with pytest.raises(ReplayError, match='metadata exceeds'):
        record(io.BytesIO(('\n'.join(json.dumps(x) for x in messages) + '\n').encode()), tmp_path / 'oversize')
    manifest = json.loads((tmp_path / 'oversize' / 'manifest.json').read_text())
    assert manifest['complete'] is False
