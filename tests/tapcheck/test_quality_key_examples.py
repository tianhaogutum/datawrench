"""Bounded per-path occurrence samples preserve value-free physical locations."""
from copy import deepcopy
import gzip
import io
import json
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft202012Validator, ValidationError
import pytest

from datawrench.tapcheck.engine import Validator
from .test_quality_paths import singer, record, catalog, ab_record, STATE


def run(messages, source='capture.jsonl', **options):
    raw = messages if isinstance(messages, bytes) else ''.join(json.dumps(m) + '\n' for m in messages).encode()
    return Validator(quality=True, **options).run(io.BytesIO(raw), source=source)


def example(kind, line, source='capture.jsonl'):
    return {'kind': kind, 'source': source, 'line': line}


def assert_sample(item, expected):
    assert item['examples'] == expected
    assert item['omitted_examples'] == item['missing_fields'] + item['null_fields'] - len(expected)


@pytest.mark.parametrize('limit', [0, 1, 100])
def test_late_component_locations_survive_full_general_sample(limit):
    report = run([singer(['early', 'late']), record({'early': None, 'late': 7}),
                  record({'early': 'KEY_VALUE_DO_NOT_COPY'}), record({'early': 7, 'late': None}), STATE],
                 example_limit=1, key_example_limit=limit)
    users = report['streams'][0]
    early, late = users['keys']['by_path']
    assert_sample(early, [example('null', 2)][:limit])
    assert_sample(late, [example('missing', 3), example('null', 4)][:limit])
    assert report['key_example_limit'] == limit
    assert users['examples'] == [{'code': 'TC010', 'source': 'capture.jsonl', 'line': 2, 'path': ['early']}]
    assert users['omitted_examples'] == 2 and users['keys']['records_with_missing_or_null_keys'] == 3
    assert report['exit_code'] == 1 and report['coverage'] == 'complete_input'
    assert 'KEY_VALUE_DO_NOT_COPY' not in json.dumps(report)


@pytest.mark.parametrize('limit', [0, 1, 100])
def test_retained_state_bound_and_exact_counters_for_repeated_paths(limit):
    messages = [singer(['id', 'id']), *[record({'id': None} if n % 2 else {}) for n in range(1001)], STATE]
    engine = Validator(quality=True, example_limit=0, key_example_limit=limit)
    report = engine.run(io.BytesIO(''.join(json.dumps(m) + '\n' for m in messages).encode()))
    paths = report['streams'][0]['keys']['by_path']
    for item in paths:
        assert item['missing_fields'] == 501 and item['null_fields'] == 500
        assert_sample(item, [example('null' if n % 2 else 'missing', n + 2, '<stdin>') for n in range(limit)])
    assert sum(len(p['examples']) for p in paths) == limit * 2
    assert paths[0]['examples'] is not paths[1]['examples']
    # Inspect retained state as well as serialized output: no full candidate list.
    samples = engine.quality.streams[('users', None)].examples_by_path
    assert sum(map(len, samples.values())) == limit * 2
    assert len(samples) == (2 if limit else 0)
    assert report['streams'][0]['keys']['missing_fields'] == 1002
    assert report['streams'][0]['keys']['null_fields'] == 1000


def test_default_zero_and_empty_or_no_key_streams():
    report = run([singer(['id']), record({}), STATE])
    assert report['key_example_limit'] == 0
    assert_sample(report['streams'][0]['keys']['by_path'][0], [])
    report = run([singer(['id']), STATE], key_example_limit=100)
    assert_sample(report['streams'][0]['keys']['by_path'][0], [])
    report = run([singer([]), record({}), STATE], key_example_limit=100)
    assert report['streams'][0]['keys']['by_path'] == [] and report['exit_code'] == 0
    report = run(b'', protocol='singer', key_example_limit=100)
    assert report['streams'] == [] and report['exit_code'] == 2 and report['key_example_limit'] == 100


def test_nested_missing_null_and_complete_values_in_separate_namespaces():
    paths = [['obj'], ['obj', 'id'], ['a.b'], [''], ['é/雪']]
    first = catalog(paths, 'a')['catalog']['streams'][0]
    second = catalog(paths, 'b')['catalog']['streams'][0]
    rows = [{}, {'obj': None}, {'obj': []}, {'obj': {'id': None}}, {'obj': {'id': 0}},
            {'obj': {'id': False}}, {'obj': {'id': ''}}]
    report = run([{'type': 'CATALOG', 'catalog': {'streams': [second, first]}},
                  *[ab_record({**r, 'a.b': 0, '': False, 'é/雪': ''}, 'a') for r in rows],
                  ab_record({'obj': {'id': 'VALUE_DO_NOT_COPY'}}, 'b'),
                  {'type': 'STATE', 'state': {'data': {}}}], key_example_limit=100, example_limit=0)
    a, b = report['streams']
    assert (a['namespace'], b['namespace']) == ('a', 'b')
    assert_sample(a['keys']['by_path'][0], [example('missing', 2), example('null', 3)])
    assert_sample(a['keys']['by_path'][1], [example('missing', n) for n in [2, 3, 4]] + [example('null', 5)])
    for item in a['keys']['by_path'][2:]: assert_sample(item, [])
    for item in b['keys']['by_path'][:2]: assert_sample(item, [])
    for item in b['keys']['by_path'][2:]: assert_sample(item, [example('missing', 9)])
    assert 'VALUE_DO_NOT_COPY' not in json.dumps(report)


def test_accepted_repeated_schema_retains_samples_rejected_inputs_add_none():
    declaration = singer(['id', 'id'])
    unknown = {**record({}), 'stream': 'unknown'}
    report = run([declaration, record({}), declaration, singer(['replacement']), record(None), unknown,
                  record({'id': None}), STATE], key_example_limit=100)
    users = report['streams'][0]
    assert users['schemas'] == 2 and users['records'] == 2
    for item in users['keys']['by_path']:
        assert_sample(item, [example('missing', 2), example('null', 7)])
    assert users['errors_by_code'] == {'TC003': 1, 'TC009': 1, 'TC010': 4}


def test_schema_evaluation_failure_still_samples_keys_without_tc010():
    declaration = singer(['id', 'other'])
    declaration['schema'] = {'$ref': '#/missing'}
    report = run([declaration, record({'id': None}), STATE], key_example_limit=1, example_limit=0)
    paths = report['streams'][0]['keys']['by_path']
    assert_sample(paths[0], [example('null', 2)])
    assert_sample(paths[1], [example('missing', 2)])
    assert report['errors_by_code'] == {'TC004': 1}
    assert 'checks_after_schema_evaluation_failure' in report['not_evaluated']


@pytest.mark.parametrize('compression', [None, 'gzip'])
def test_batch_uses_physical_row_locations_and_restores_inline_source(tmp_path, compression):
    raw = b'\n{}\nBAD_JSON\n{"id":null}\n{"id":"VALUE_DO_NOT_COPY"}\n\n{}\n'
    name = 'nested/rows.jsonl' + ('.gz' if compression else '')
    path = tmp_path / name
    path.parent.mkdir()
    original = gzip.compress(raw) if compression else raw
    path.write_bytes(original)
    encoding = {'format': 'jsonl', 'compression': compression}
    batch = {'type': 'BATCH', 'stream': 'users', 'encoding': encoding, 'manifest': [name, name]}
    report = run([singer(['id']), batch, record({'id': None}), STATE],
                 batch_root=tmp_path, key_example_limit=100, example_limit=1)
    item = report['streams'][0]['keys']['by_path'][0]
    assert_sample(item, [example('missing', 2, name), example('null', 4, name), example('missing', 7, name)] * 2
                  + [example('null', 3)])
    assert report['streams'][0]['records'] == 9 and report['exit_code'] == 1
    assert report['coverage'] == 'complete_input' and 'VALUE_DO_NOT_COPY' not in json.dumps(report)
    assert path.read_bytes() == original
    assert (gzip.decompress(path.read_bytes()) if compression else path.read_bytes()) == raw


def test_partial_batch_stop_samples_only_counted_prefix(tmp_path):
    (tmp_path / 'rows.jsonl').write_text('{}\n{"id":1}\n{"id":2}\n{"id":null}\n')
    batch = {'type': 'BATCH', 'stream': 'users', 'encoding': {'format': 'jsonl'}, 'manifest': ['rows.jsonl']}
    report = run([singer(['id']), batch, record({'id': None}), STATE], batch_root=tmp_path,
                 key_example_limit=100, check_duplicates=True, duplicate_limit=1)
    assert_sample(report['streams'][0]['keys']['by_path'][0], [example('missing', 1, 'rows.jsonl')])
    assert report['streams'][0]['records'] == 3
    assert report['exit_code'] == 2 and report['stopped_early'] and report['coverage'] == 'partial_input'
    assert report['errors_by_code'] == {'TC010': 1, 'TC018': 1}


@pytest.mark.parametrize('limit', ['-1', '101', '1.5', 'no', '1e2'])
def test_cli_rejects_invalid_key_example_limit(limit):
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', 'quality', '--key-example-limit', limit],
                            input=b'', capture_output=True)
    assert result.returncode == 2 and result.stdout == b''
    assert b'--key-example-limit' in result.stderr and b'Traceback' not in result.stderr


@pytest.mark.parametrize('limit', [-1, 101, 1.5, '1', None, True])
def test_api_rejects_invalid_key_example_limit(limit):
    with pytest.raises(ValueError, match='key_example_limit'):
        Validator(quality=True, key_example_limit=limit)


@pytest.mark.parametrize('limit', [0, 1, 100])
def test_cli_catalog_stdin_and_unopened_input_reports_setting(tmp_path, limit):
    cat = tmp_path / 'catalog.json'
    cat.write_text(json.dumps({'streams': [{'stream': 'users', 'schema': {'type': 'object'}, 'key_properties': ['id']}]}))
    args = [sys.executable, '-m', 'datawrench', 'tapcheck', 'quality', '--protocol', 'singer', '--catalog', str(cat),
            '--key-example-limit', str(limit)]
    result = subprocess.run(args, input=(json.dumps(record({})) + '\n' + json.dumps(STATE) + '\n').encode(), capture_output=True)
    assert result.returncode == 1 and result.stderr == b''
    report = json.loads(result.stdout)
    assert report['key_example_limit'] == limit
    assert_sample(report['streams'][0]['keys']['by_path'][0], [example('missing', 1, '-')][:limit])
    result = subprocess.run([*args, str(tmp_path / 'absent.jsonl')], capture_output=True)
    report = json.loads(result.stdout)
    assert result.returncode == 2 and report['key_example_limit'] == limit and report['streams'] == []


def test_report_schema_rejects_non_location_data_and_bad_sample_shapes():
    schema = json.loads(Path('docs/tapcheck/quality-report.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    report = run([singer(['id']), record({}), STATE], key_example_limit=1)
    validator.validate(report)
    for bad in [None, {}, [example('value', 2)], [example('missing', 0)], [example('null', True)],
                [example('null', 1.5)], [example('null', 2, None)], [{'kind': 'missing', 'line': 2}],
                [{**example('missing', 2), 'value': 'DO_NOT_COPY'}], [example('missing', 2)] * 101]:
        invalid = deepcopy(report)
        invalid['streams'][0]['keys']['by_path'][0]['examples'] = bad
        with pytest.raises(ValidationError): validator.validate(invalid)
    for field, values in [('omitted_examples', [-1, True, '1', None]), ('examples', [None])]:
        for bad in values:
            invalid = deepcopy(report)
            invalid['streams'][0]['keys']['by_path'][0][field] = bad
            with pytest.raises(ValidationError): validator.validate(invalid)
        invalid = deepcopy(report)
        del invalid['streams'][0]['keys']['by_path'][0][field]
        with pytest.raises(ValidationError): validator.validate(invalid)
    for bad in [-1, 101, True, '1', None]:
        invalid = deepcopy(report)
        invalid['key_example_limit'] = bad
        with pytest.raises(ValidationError): validator.validate(invalid)
    invalid = deepcopy(report)
    del invalid['key_example_limit']
    with pytest.raises(ValidationError): validator.validate(invalid)


def test_main_input_blank_lines_use_physical_locations():
    raw = (json.dumps(singer(['id'])) + '\n\n' + json.dumps(record({})) + '\n\n' + json.dumps(STATE)).encode()
    report = run(raw, source='relative/雪.jsonl', key_example_limit=1)
    assert_sample(report['streams'][0]['keys']['by_path'][0], [example('missing', 3, 'relative/雪.jsonl')])
