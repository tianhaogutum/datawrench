"""Synthetic Singer integration fixtures for explicit object scrub paths."""
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
def nested_messages(messages):
    result = [m for m in messages if m.get('stream') == 'customers']
    profile = result[0]['schema']['properties']['profile']
    phone = profile['properties'].pop('phone')
    phone.update(type=['string', 'null'], examples=['555-0100'], default='555-0100')
    profile['properties']['contact'] = {
        'type': 'object', 'properties': {'phone': phone, 'public': {'type': 'boolean'}},
        'required': ['phone', 'public'], 'examples': [{'phone': '555-0100'}]}
    profile['required'] = ['city', 'contact']
    profile['default'] = {'contact': {'phone': '555-0100'}}
    result[0]['schema']['examples'] = [{'profile': {'contact': {'phone': '555-0100'}}}]
    for i, msg in enumerate(result):
        msg['stream'] = 'audit.customers'
        if msg['type'] == 'RECORD':
            p = msg['record']['profile']
            phone = p.pop('phone')
            p['contact'] = {'phone': phone if i < 3 else None, 'public': True}
    return result


@pytest.mark.parametrize('mode', ['hash', 'mask', 'drop'])
def test_nested_scrub_preserves_siblings_schema_and_integrity(nested_messages, make_cassette, mode):
    original = copy.deepcopy(nested_messages)
    rules = scrub_rules([f'audit.customers:profile.contact.phone={mode}'])
    path = make_cassette(nested_messages, rules=rules, salt=b'N' * 32)
    same = make_cassette(nested_messages, rules=rules, salt=b'N' * 32)
    assert nested_messages == original
    assert (path / 'messages.jsonl.gz').read_bytes() == (same / 'messages.jsonl.gz').read_bytes()
    with Cassette(path) as cassette:
        output = list(cassette.messages())
        assert cassette.catalog['streams'][0]['schema'] == output[0]['schema']
        assert cassette.manifest['scrub'] == [{
            'stream': 'audit.customers', 'path': ['profile', 'contact', 'phone'], 'mode': mode}]
        assert cassette.manifest['counts'] == {'audit.customers': 3}
        for name, digest in cassette.manifest['sha256'].items():
            assert hashlib.sha256((path / name).read_bytes()).hexdigest() == digest
    profile = output[0]['schema']['properties']['profile']
    contact = profile['properties']['contact']
    assert profile['required'] == ['city', 'contact']
    assert profile['properties']['city'] == original[0]['schema']['properties']['profile']['properties']['city']
    assert 'examples' not in output[0]['schema']
    assert 'default' not in profile and 'examples' not in contact
    if mode == 'drop':
        assert contact['required'] == ['public'] and 'phone' not in contact['properties']
    else:
        assert contact['required'] == ['phone', 'public']
        assert contact['properties']['phone'] == {'type': ['string', 'null']}
    rows = [m['record'] for m in output if m['type'] == 'RECORD']
    expected = hmac.new(b'N' * 32, json.dumps(
        ['audit.customers', ['profile', 'contact', 'phone'], '555-0100'],
        separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
    for index, row in enumerate(rows):
        assert row['profile']['city'] == 'Demo City'
        assert row['profile']['contact']['public'] is True
        assert row['id'] == index + 1
        if mode == 'drop':
            assert row['profile']['contact'] == {'public': True}
        else:
            assert row['profile']['contact']['phone'] == (
                None if index == 2 else '***' if mode == 'mask' else expected)
    for file in path.iterdir():
        data = gzip.decompress(file.read_bytes()) if file.suffix == '.gz' else file.read_bytes()
        assert b'555-0100' not in data


@pytest.mark.parametrize('rule, error', [
    ('customers:profile.missing=mask', 'absent from stream schema'),
    ('customers:missing.phone=mask', 'absent from stream schema'),
    ('customers:email.child=hash', 'non-object schema'),
    ('customers:tags.label=drop', 'non-object schema'),
    ('customers:id=drop', 'primary/replication keys'),
    ('customers:updated_at=hash', 'primary/replication keys'),
    ('customers:id.child=mask', 'primary/replication keys'),
    ('customers:updated_at.child=drop', 'primary/replication keys'),
    ('missing:profile.phone=hash', 'absent stream'),
])
def test_invalid_scrub_paths_leave_incomplete_cassettes(messages, tmp_path, rule, error):
    path = tmp_path / 'bad-path'
    with pytest.raises(ReplayError, match=error):
        record(io.BytesIO(('\n'.join(json.dumps(m) for m in messages) + '\n').encode()),
               path, rules=scrub_rules([rule]))
    assert json.loads((path / 'manifest.json').read_text())['complete'] is False
    with pytest.raises(ReplayError, match='incomplete'):
        with Cassette(path):
            pass


@pytest.mark.parametrize('missing', ['leaf', 'parent', 'null_parent'])
def test_missing_record_paths_are_errors(messages, tmp_path, missing):
    profile = messages[0]['schema']['properties']['profile']
    if missing == 'leaf':
        profile['required'].remove('phone')
        del messages[2]['record']['profile']['phone']
    elif missing == 'parent':
        del messages[2]['record']['profile']
    else:
        messages[2]['record']['profile'] = None
    path = tmp_path / 'missing-path'
    error = 'non-object record' if missing == 'null_parent' else 'absent from record'
    with pytest.raises(ReplayError, match=error):
        record(io.BytesIO(('\n'.join(json.dumps(m) for m in messages) + '\n').encode()),
               path, rules=scrub_rules(['customers:profile.phone=mask']))
    assert json.loads((path / 'manifest.json').read_text())['complete'] is False


@pytest.mark.parametrize('rules', [
    ['customers:profile.phone=hash', 'customers:profile.phone=mask'],
    ['customers.profile=hash', 'customers:profile.phone=mask'],
    ['customers:profile.phone=mask', 'customers.profile=drop'],
    ['customers.email=hash', 'customers:email=hash'],
])
def test_overlapping_scrub_rules_are_rejected(rules):
    with pytest.raises(ReplayError, match='duplicate|overlapping'):
        scrub_rules(rules)


@pytest.mark.parametrize('rule', [
    'customers:profile.*=mask', 'customers:profile..phone=hash',
    'customers:.phone=drop', 'customers:profile.=mask',
    'customers:profile[0].phone=mask', 'customers:profile.__call__()=hash',
    'customers:profile/phone=hash', 'customers::phone=mask',
    'customers:' + '.'.join(['a'] * 33) + '=mask',
])
def test_restricted_path_syntax(rule):
    with pytest.raises(ReplayError, match='scrub'):
        scrub_rules([rule])


def test_sibling_rules_and_legacy_dotted_names(messages, make_cassette):
    for msg in messages:
        if msg.get('stream') == 'customers':
            msg['stream'] = 'audit.customers'
        elif msg['type'] == 'STATE':
            msg['value']['bookmarks']['audit.customers'] = msg['value']['bookmarks'].pop('customers')
    rules = scrub_rules(['audit.customers:profile.phone=mask',
                         'audit.customers:profile.city=drop',
                         'audit.customers.email=hash'])
    with Cassette(make_cassette(messages, rules=rules)) as cassette:
        for msg in cassette.messages():
            if msg['type'] == 'RECORD' and msg['stream'] == 'audit.customers':
                assert msg['record']['profile'] == {'phone': '***'}
                assert len(msg['record']['email']) == 64


def test_repeated_component_names_and_hash_domains(make_cassette):
    messages = [
        {'type': 'SCHEMA', 'stream': 'events', 'key_properties': [], 'schema': {
            'type': 'object', 'properties': {
                'a': {'type': 'object', 'properties': {'a': {'type': 'string'}}},
                'b': {'type': 'object', 'properties': {'a': {'type': 'string'}}}}}},
        {'type': 'RECORD', 'stream': 'events', 'record': {'a': {'a': 'same'}, 'b': {'a': 'same'}}}]
    rules = scrub_rules(['events:a.a=hash', 'events:b.a=hash'])
    path = make_cassette(messages, rules=rules, salt=b'D' * 32)
    other = make_cassette(messages, rules=rules, salt=b'E' * 32)
    with Cassette(path) as cassette:
        row = list(cassette.messages())[1]['record']
        assert len(row['a']['a']) == 64 and row['a']['a'] != row['b']['a']
    assert (path / 'messages.jsonl.gz').read_bytes() != (other / 'messages.jsonl.gz').read_bytes()


def test_explicit_top_level_rules_match_legacy_hashes(messages, make_cassette):
    old = make_cassette(messages, rules=scrub_rules(['customers.email=hash']), salt=b'C' * 32)
    new = make_cassette(messages, rules=scrub_rules(['customers:email=hash']), salt=b'C' * 32)
    assert (old / 'messages.jsonl.gz').read_bytes() == (new / 'messages.jsonl.gz').read_bytes()
