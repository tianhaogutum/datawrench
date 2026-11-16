"""JSON Pointer contract: literal object names, strict escapes, shared scrub safety."""
import copy
import gzip
import hashlib
import hmac
import io
import json

import pytest

from datawrench.singer_replay.cassette import Cassette, record, scrub_rules
from datawrench.singer_replay.common import ReplayError


@pytest.fixture
def pointer_messages():
    row = {
        'id.key': 1, '更新/時刻': 10,
        'profile.info': {'電話/番号': 'secret-phone', 'keep': True},
        'profile': {'info': 'untouched'}, 'a~b': 'secret-tilde',
        '~1': 'secret-once', 'equals=colon:': 'secret-delimiter',
        'nullable': None, '0': 'secret-numeric', '%2F': 'secret-percent',
    }
    props = {
        name: {'type': 'integer' if type(value) is int else
               'object' if isinstance(value, dict) else
               ['string', 'null'] if value is None else 'string'}
        for name, value in row.items()
    }
    props['profile.info'].update(
        properties={'電話/番号': {'type': 'string', 'examples': ['secret-phone']},
                    'keep': {'type': 'boolean'}},
        required=['電話/番号', 'keep'], default={'電話/番号': 'secret-phone'})
    props['profile'].update(properties={'info': {'type': 'string'}})
    return [
        {'type': 'SCHEMA', 'stream': 'audit.events', 'key_properties': ['id.key'],
         'replication_key': '更新/時刻', 'schema': {
             'type': 'object', 'properties': props, 'required': list(row),
             'examples': [copy.deepcopy(row)]}},
        {'type': 'RECORD', 'stream': 'audit.events', 'record': row},
    ]


@pytest.mark.parametrize('mode', ['hash', 'mask', 'drop'])
def test_pointer_literal_names_round_trip(pointer_messages, make_cassette, mode):
    original = copy.deepcopy(pointer_messages)
    names = [('profile.info', '電話/番号'), ('a~b',), ('~1',),
             ('equals=colon:',), ('nullable',), ('0',), ('%2F',)]
    tokens = ['/profile.info/電話~1番号', '/a~0b', '/~01',
              '/equals=colon:', '/nullable', '/0', '/%2F']
    rules = scrub_rules([f'audit.events:{p}={mode}' for p in tokens])
    path = make_cassette(pointer_messages, rules=rules, salt=b'P' * 32)
    assert pointer_messages == original
    with Cassette(path) as cassette:
        output = list(cassette.messages())
        assert cassette.manifest['scrub'] == [
            {'stream': 'audit.events', 'path': list(p), 'mode': mode} for p in names]
        assert cassette.catalog['streams'][0]['schema'] == output[0]['schema']
    schema, row = output[0]['schema'], output[1]['record']
    assert row['id.key'] == 1 and row['更新/時刻'] == 10
    assert row['profile'] == {'info': 'untouched'}
    assert row['profile.info']['keep'] is True
    assert 'examples' not in schema
    assert 'default' not in schema['properties']['profile.info']
    for parts in names:
        before = original[1]['record']
        after, parent_schema = row, schema
        for part in parts[:-1]:
            before, after = before[part], after[part]
            parent_schema = parent_schema['properties'][part]
        leaf = parts[-1]
        if mode == 'drop':
            assert leaf not in after and leaf not in parent_schema['properties']
            assert leaf not in parent_schema.get('required', [])
        else:
            expected = None if before[leaf] is None else '***'
            if mode == 'hash' and before[leaf] is not None:
                domain = parts[0] if len(parts) == 1 else list(parts)
                expected = hmac.new(b'P' * 32, json.dumps(
                    ['audit.events', domain, before[leaf]], ensure_ascii=False,
                    sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
            assert after[leaf] == expected
            assert parent_schema['properties'][leaf] == {'type': ['string', 'null']}
    for member in path.iterdir():
        raw = gzip.decompress(member.read_bytes()) if member.suffix == '.gz' else member.read_bytes()
        assert b'secret-' not in raw


@pytest.mark.parametrize('old,new', [
    ('customers.email=hash', 'customers:/email=hash'),
    ('customers:profile.phone=hash', 'customers:/profile/phone=hash'),
])
def test_pointer_hashes_match_existing_paths(messages, make_cassette, old, new):
    first = make_cassette(messages, rules=scrub_rules([old]), salt=b'A' * 32)
    second = make_cassette(messages, rules=scrub_rules([new]), salt=b'A' * 32)
    assert (first / 'messages.jsonl.gz').read_bytes() == (second / 'messages.jsonl.gz').read_bytes()


@pytest.mark.parametrize('rules', [
    ['customers.email=hash', 'customers:/email=mask'],
    ['customers:profile.phone=hash', 'customers:/profile/phone=drop'],
    ['customers.profile=drop', 'customers:/profile/phone=hash'],
    ['customers:/profile/phone=hash', 'customers:profile=mask'],
    ['audit.events:/profile.info=mask', 'audit.events:/profile.info/電話~1番号=drop'],
    ['audit.events:/a~1b=mask', 'audit.events:/a~1b/child=hash'],
])
def test_pointer_aliases_and_overlaps_are_rejected(rules):
    with pytest.raises(ReplayError, match='duplicate|overlapping'):
        scrub_rules(rules)


@pytest.mark.parametrize('path', [
    '', '/', '//phone', '/profile/', '/profile//phone', '/bad~', '/bad~2',
    '/bad~01~9', '#/profile/phone', '/' + '/'.join(['a'] * 33),
])
def test_invalid_pointer_syntax(path):
    with pytest.raises(ReplayError, match='scrub'):
        scrub_rules([f'customers:{path}=mask'])


@pytest.mark.parametrize('target,error', [
    ('/id.key', 'primary/replication keys'),
    ('/id.key/child', 'primary/replication keys'),
    ('/更新~1時刻', 'primary/replication keys'),
    ('/更新~1時刻/child', 'primary/replication keys'),
    ('/profile.info/missing', 'absent from stream schema'),
    ('/profile/info/child', 'non-object schema'),
    ('/profile.info/*', 'absent from stream schema'),
    ('/profile.info/電話[0]', 'absent from stream schema'),
    ('/profile.info/__call__()', 'absent from stream schema'),
])
def test_pointer_invalid_targets_leave_incomplete(pointer_messages, tmp_path, target, error):
    path = tmp_path / 'bad'
    with pytest.raises(ReplayError, match=error):
        record(io.BytesIO(('\n'.join(json.dumps(m) for m in pointer_messages) + '\n').encode()),
               path, rules=scrub_rules([f'audit.events:{target}=mask']))
    assert json.loads((path / 'manifest.json').read_text())['complete'] is False
    with pytest.raises(ReplayError, match='incomplete'):
        with Cassette(path):
            pass


@pytest.mark.parametrize('boundary', ['missing_leaf', 'missing_parent', 'null_parent', 'array'])
def test_pointer_record_and_array_boundaries(pointer_messages, tmp_path, boundary):
    schema = pointer_messages[0]['schema']
    row = pointer_messages[1]['record']
    if boundary == 'missing_leaf':
        schema['properties']['profile.info']['required'].remove('電話/番号')
        del row['profile.info']['電話/番号']
    elif boundary == 'missing_parent':
        schema['required'].remove('profile.info')
        del row['profile.info']
    elif boundary == 'null_parent':
        schema['properties']['profile.info']['type'] = ['object', 'null']
        row['profile.info'] = None
    else:
        schema['properties']['profile.info'] = {
            'type': 'array', 'items': schema['properties']['profile.info']}
        row['profile.info'] = [row['profile.info']]
    target = '/profile.info/0/電話~1番号' if boundary == 'array' else '/profile.info/電話~1番号'
    error = 'non-object' if boundary in ('null_parent', 'array') else 'absent from record'
    path = tmp_path / 'bad'
    with pytest.raises(ReplayError, match=error):
        record(io.BytesIO(('\n'.join(json.dumps(m) for m in pointer_messages) + '\n').encode()),
               path, rules=scrub_rules([f'audit.events:{target}=mask']))
    assert json.loads((path / 'manifest.json').read_text())['complete'] is False


def test_pointer_component_bound_and_decode_once():
    parts = ['a'] * 32
    assert scrub_rules(['events:/' + '/'.join(parts) + '=mask']) == {('events', tuple(parts)): 'mask'}
    assert scrub_rules(['events:/~01/~10/~00=mask']) == {('events', ('~1', '/0', '~0')): 'mask'}
