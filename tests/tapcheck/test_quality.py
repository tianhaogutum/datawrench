"""Synthetic Singer/Airbyte integration cases for value-free quality reports."""
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from datawrench.tapcheck.engine import Validator


def schema(name='users', keys=None):
    return {'type': 'SCHEMA', 'stream': name, 'schema': {'type': 'object'},
            'key_properties': ['id'] if keys is None else keys}


def record(data, name='users'):
    return {'type': 'RECORD', 'stream': name, 'record': data}


STATE = {'type': 'STATE', 'value': {'secret': 'STATE_VALUE_DO_NOT_COPY'}}


def quality(messages, **options):
    raw = messages if isinstance(messages, bytes) else ''.join(json.dumps(m) + '\n' for m in messages).encode()
    return Validator(quality=True, **options).run(io.BytesIO(raw))


def stream(report, name='users', namespace=None):
    return next(s for s in report['streams'] if (s['name'], s['namespace']) == (name, namespace))


def test_explicit_example_and_cli():
    messages = [schema(), record({'id': 1}), record({'id': 1}), record({}), STATE]
    report = quality(messages, check_duplicates=True)
    users = stream(report)
    assert (users['schemas'], users['records'], users['states']) == (1, 3, 1)
    assert users['keys'] == {'paths': [['id']], 'missing_fields': 1, 'null_fields': 0,
                            'by_path': [{'path': ['id'], 'missing_fields': 1, 'null_fields': 0,
                                         'examples': [], 'omitted_examples': 1}],
                            'records_with_missing_or_null_keys': 1}
    assert users['duplicates'] == {'enabled': True, 'evaluated_records': 2, 'duplicate_keys': 1}
    assert users['errors_by_code'] == {'TC010': 1, 'TC016': 1}
    assert [(e['code'], e['line'], e['path']) for e in users['examples']] == [
        ('TC016', 3, None), ('TC010', 4, ['id'])]
    assert report['exit_code'] == 1 and report['coverage'] == 'complete_input'
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', 'quality', '--check-duplicates'],
                            input=''.join(json.dumps(m) + '\n' for m in messages).encode(), capture_output=True)
    assert result.returncode == 1 and result.stderr == b''
    for example in users['examples']:
        example['source'] = '-'
    assert json.loads(result.stdout) == report


def test_large_invalid_stream_counts_beyond_diagnostic_limit_and_bounds():
    raw = json.dumps(schema(keys=['id', 'other'])).encode() + b'\n'
    raw += b'{"type":"RECORD","stream":"users","record":{"id":null}}\n' * 10_001
    raw += json.dumps(STATE).encode() + b'\n'
    report = quality(raw, example_limit=2)
    users = stream(report)
    assert users['records'] == 10_001 and users['states'] == 1
    assert users['keys']['missing_fields'] == users['keys']['null_fields'] == 10_001
    assert users['keys']['by_path'] == [
        {'path': ['id'], 'missing_fields': 0, 'null_fields': 10_001, 'examples': [], 'omitted_examples': 10_001},
        {'path': ['other'], 'missing_fields': 10_001, 'null_fields': 0, 'examples': [], 'omitted_examples': 10_001}]
    assert users['keys']['records_with_missing_or_null_keys'] == 10_001
    assert users['errors_by_code'] == {'TC010': 20_002}
    assert report['errors'] == 20_002 and not report['stopped_early']
    assert len(users['examples']) == 2 and users['omitted_examples'] == 20_000
    assert report == quality(raw, example_limit=2)


def test_multiple_and_empty_streams_sorted_with_valid_state_counts():
    messages = [schema('z'), schema('a'), schema('z'), record({}, 'z'),
                {'type': 'STATE', 'value': []}, STATE, STATE]
    report = quality(messages)
    assert [s['name'] for s in report['streams']] == ['a', 'z']
    assert (stream(report, 'a')['schemas'], stream(report, 'a')['records'], stream(report, 'a')['states']) == (1, 0, 2)
    assert (stream(report, 'z')['schemas'], stream(report, 'z')['records'], stream(report, 'z')['states']) == (2, 1, 2)
    assert report['unassigned']['errors_by_code'] == {'TC007': 1}
    assert report['messages'] == {'SCHEMA': 3, 'RECORD': 1, 'STATE': 3}
    assert stream(report, 'a')['duplicates']['duplicate_keys'] is None
    assert 'duplicate_keys' in report['not_evaluated']


def ab_entry(namespace):
    return {'name': 'users', 'namespace': namespace, 'json_schema': {'type': 'object'},
            'source_defined_primary_key': [['obj', 'id'], ['other']]}


def ab_state(kind, namespaces):
    states = [{'stream_descriptor': {'name': 'users', 'namespace': ns}, 'stream_state': None} for ns in namespaces]
    return {'type': 'STATE', 'state': {'type': kind, **({'stream': states[0]} if kind == 'STREAM' else
            {'global': {'stream_states': states}} if kind == 'GLOBAL' else {'data': None})}}


def test_airbyte_nested_composite_keys_namespaces_and_state_deduplication():
    messages = [{'type': 'CATALOG', 'catalog': {'streams': [ab_entry('b'), ab_entry('a'), ab_entry(None)]}}]
    for ns, data in [('a', {'obj': {'id': 1}, 'other': 2}), ('b', {'obj': {'id': 1}, 'other': 2}),
                     ('a', {'obj': {'id': 1.0}, 'other': 2}), ('a', {'obj': {}, 'other': None})]:
        messages.append({'type': 'RECORD', 'record': {'stream': 'users', 'namespace': ns, 'data': data, 'emitted_at': 0}})
    messages += [ab_state('STREAM', ['a']), ab_state('GLOBAL', ['b', 'b']), ab_state('LEGACY', [])]
    report = quality(messages, check_duplicates=True)
    assert [s['namespace'] for s in report['streams']] == [None, 'a', 'b']
    assert [s['states'] for s in report['streams']] == [1, 2, 2]
    assert [s['schemas'] for s in report['streams']] == [1, 1, 1]
    assert stream(report, namespace='a')['duplicates']['duplicate_keys'] == 1
    assert stream(report, namespace='b')['duplicates']['duplicate_keys'] == 0
    assert stream(report, namespace='a')['keys']['missing_fields'] == 1
    assert stream(report, namespace='a')['keys']['null_fields'] == 1
    assert stream(report, namespace='a')['examples'][1]['path'] == ['obj', 'id']


def test_local_batches_and_activate_preserve_boundaries(tmp_path):
    (tmp_path / 'rows.jsonl').write_text('{"id":1}\n{"id":1}\n{}\n')
    batch = {'type': 'BATCH', 'stream': 'users', 'encoding': {'format': 'jsonl'}, 'manifest': ['rows.jsonl']}
    messages = [schema(), batch, {'type': 'ACTIVATE_VERSION', 'stream': 'users', 'version': 2},
                record({'id': 1}), STATE]
    report = quality(messages, check_duplicates=True, batch_root=tmp_path)
    users = stream(report)
    assert users['records'] == 4 and users['duplicates']['duplicate_keys'] == 2
    assert [(e['source'], e['line']) for e in users['examples']] == [
        ('rows.jsonl', 2), ('rows.jsonl', 3), ('<stdin>', 4)]
    assert {'version_delete_semantics', 'remote_or_non_jsonl_batches'} <= set(report['not_evaluated'])
    batch['encoding']['format'] = 'parquet'
    assert quality([schema(), batch, STATE], batch_root=tmp_path)['exit_code'] == 1


def test_values_not_copied_and_key_counts_survive_schema_evaluation_failure():
    declaration = schema()
    declaration['schema'] = {'properties': {'secret': {'enum': ['SCHEMA_VALUE_DO_NOT_COPY']}}, '$ref': '#/missing'}
    declaration['bookmark_properties'] = ['cursor']
    report = quality([declaration, record({'cursor': 'CURSOR_VALUE_DO_NOT_COPY',
                                        'secret': 'RECORD_VALUE_DO_NOT_COPY'}), STATE], check_duplicates=True)
    assert stream(report)['records'] == 1 and stream(report)['keys']['missing_fields'] == 1
    assert stream(report)['duplicates']['evaluated_records'] == 0
    assert 'checks_after_schema_evaluation_failure' in report['not_evaluated']
    assert 'DO_NOT_COPY' not in json.dumps(report)


@pytest.mark.parametrize('options,code', [({'duplicate_limit': 1, 'check_duplicates': True}, 'TC018'),
                                        ({'max_properties': 1}, 'TC020'), ({'max_streams': 1}, 'TC020')])
def test_capacity_failures_are_partial_not_certification(options, code):
    report = quality([schema(), record({'id': 1}), record({'id': 2, 'x': 1}), schema('other'), STATE], **options)
    assert report['exit_code'] == 2 and report['coverage'] == 'partial_input' and report['stopped_early']
    assert report['errors_by_code'][code] == 1
    assert 'remaining_input_after_stop' in report['not_evaluated']


@pytest.mark.parametrize('raw,code,exit_code', [(b'', 'TC021', 2), (b'{\n', 'TC001', 1)])
def test_empty_or_malformed_input_has_bounded_unassigned_examples(raw, code, exit_code):
    report = quality(raw, protocol='singer', example_limit=0)
    assert report['streams'] == [] and report['exit_code'] == exit_code
    assert report['unassigned'] == {'errors_by_code': {code: 1}, 'examples': [], 'omitted_examples': 1}


def test_cli_io_failure_and_external_catalog_report_schema(tmp_path):
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'streams': [dict(schema(), tap_stream_id='users')]}))
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', 'quality', '--catalog', str(catalog)],
                            input=json.dumps(STATE).encode(), capture_output=True)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert stream(report)['schemas'] == stream(report)['states'] == 1
    report_schema = json.loads(Path('docs/tapcheck/quality-report.schema.json').read_text())
    Draft202012Validator.check_schema(report_schema)
    Draft202012Validator(report_schema).validate(report)
    for path in [tmp_path / 'missing', catalog]:
        args = ['quality', str(path)] if path != catalog else ['quality', '--catalog', str(path)]
        if path == catalog:
            catalog.write_text('bad json')
        result = subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', *args], input=b'', capture_output=True)
        assert result.returncode == 2 and result.stderr == b''
        report = json.loads(result.stdout)
        assert report['coverage'] == 'partial_input'
        assert report['unassigned']['examples'][0]['source'] == (str(path) if path != catalog else '-')
        Draft202012Validator(report_schema).validate(report)


def test_value_free_valid_cursor_statistics_and_missing_state():
    declaration = schema(keys=[])
    declaration['bookmark_properties'] = ['cursor']
    report = quality([declaration, record({'cursor': 'CURSOR_VALUE_DO_NOT_COPY',
                                         'secret': 'RECORD_VALUE_DO_NOT_COPY'})], check_duplicates=True)
    assert stream(report)['records'] == 1
    assert stream(report)['errors_by_code'] == {'TC008': 1}
    assert stream(report)['duplicates']['duplicate_keys'] is None
    assert 'streams_without_primary_keys' in report['not_evaluated']
    assert 'DO_NOT_COPY' not in json.dumps(report)


def test_changed_contract_malformed_record_and_schema_failure_counts():
    changed = schema(keys=['other'])
    report = quality([schema(), changed, record([]), record({}), STATE])
    assert stream(report)['schemas'] == 1 and stream(report)['records'] == 1
    assert stream(report)['keys']['paths'] == [['id']]
    assert stream(report)['errors_by_code'] == {'TC003': 1, 'TC009': 1, 'TC010': 1}
    # Ordinary JSON Schema violations do not suppress key or duplicate checking.
    declaration = schema()
    declaration['schema'] = {'required': ['required_field']}
    report = quality([declaration, record({'id': 1}), record({'id': 1}), STATE], check_duplicates=True)
    assert stream(report)['errors_by_code'] == {'TC006': 2, 'TC016': 1}
    assert stream(report)['duplicates']['evaluated_records'] == 2


def test_unassigned_examples_default_bound_and_unsupported_message_storage():
    messages = [{'type': f'UNKNOWN_{i}'} for i in range(1000)]
    report = quality(messages, protocol='singer')
    assert report['messages'] == {'UNSUPPORTED': 1000}
    assert report['errors'] == 1000 and report['exit_code'] == 1
    assert len(report['unassigned']['examples']) == 5 and report['unassigned']['omitted_examples'] == 995
    assert report['unassigned']['examples'][0] == {'code': 'TC002', 'source': '<stdin>', 'line': 1, 'path': None}


def test_line_limit_and_mixed_protocol_preserve_reporting_boundaries():
    raw = b'x' * 201 + b'\n' + json.dumps(STATE).encode() + b'\n'
    report = quality(raw, protocol='singer', max_line_bytes=200)
    assert report['messages'] == {'STATE': 1} and report['errors_by_code'] == {'TC020': 1}
    assert 'unparsed_or_oversized_input' in report['not_evaluated']
    report = quality([schema(), {'type': 'CATALOG', 'catalog': {'streams': []}}])
    assert report['errors_by_code'] == {'TC022': 1} and report['coverage'] == 'partial_input'


@pytest.mark.parametrize('limit', ['-1', '101', 'not-a-number'])
def test_cli_rejects_invalid_example_limit(limit):
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', 'quality', '--example-limit', limit], capture_output=True)
    assert result.returncode == 2 and b'usage:' in result.stderr


def test_quality_corrupt_gzip_keeps_json_and_continues(tmp_path):
    (tmp_path / 'bad.gz').write_bytes(bytes.fromhex('1f8b0800000000000203') + b'\x07' + bytes(8))
    batch = {'type': 'BATCH', 'stream': 'users', 'encoding': {'format': 'jsonl', 'compression': 'gzip'},
             'manifest': ['bad.gz']}
    report = quality([schema(), batch, record({'id': 1}), STATE], batch_root=tmp_path)
    assert stream(report)['records'] == stream(report)['states'] == 1
    assert stream(report)['errors_by_code'] == {'TC017': 1}
    assert 'unreadable_or_unsupported_batch_contents' in report['not_evaluated']


def test_large_duplicate_stream_keeps_exact_counts_with_one_digest():
    raw = json.dumps(schema()).encode() + b'\n'
    raw += b'{"type":"RECORD","stream":"users","record":{"id":1}}\n' * 10_001
    raw += json.dumps(STATE).encode() + b'\n'
    report = quality(raw, check_duplicates=True, duplicate_limit=1)
    users = stream(report)
    assert users['duplicates'] == {'enabled': True, 'evaluated_records': 10_001, 'duplicate_keys': 10_000}
    assert users['records'] == 10_001 and users['states'] == 1
    assert len(users['examples']) == 5 and users['omitted_examples'] == 9995
    assert report['coverage'] == 'complete_input' and report['exit_code'] == 1


def test_quality_schema_covers_key_examples_and_partial_reports():
    report_schema = json.loads(Path('docs/tapcheck/quality-report.schema.json').read_text())
    validator = Draft202012Validator(report_schema)
    for options in [{'check_duplicates': True}, {'check_duplicates': True, 'duplicate_limit': 1}, {'example_limit': 0}]:
        report = quality([schema(), record({'id': 1}), record({'id': 1}), record({'id': 2}), record({}), STATE], **options)
        validator.validate(report)
