"""Primary-key components remain visible even when quality examples are full."""
from copy import deepcopy
import io
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from datawrench.tapcheck.engine import Validator


def run(messages, **options):
    raw = ''.join(json.dumps(m) + '\n' for m in messages).encode()
    return Validator(quality=True, **options).run(io.BytesIO(raw))


def singer(keys):
    return {'type': 'SCHEMA', 'stream': 'users', 'schema': {'type': 'object'}, 'key_properties': keys}


def record(data):
    return {'type': 'RECORD', 'stream': 'users', 'record': data}


STATE = {'type': 'STATE', 'value': {}}


def catalog(paths, namespace=None):
    return {'type': 'CATALOG', 'catalog': {'streams': [
        {'name': 'users', 'namespace': namespace, 'json_schema': {'type': 'object'},
         'source_defined_primary_key': paths}]}}


def ab_record(data, namespace=None):
    return {'type': 'RECORD', 'record': {'stream': 'users', 'namespace': namespace,
                                      'data': data, 'emitted_at': 0}}


def component(path, missing=0, null=0):
    return {'path': path, 'missing_fields': missing, 'null_fields': null,
            'examples': [], 'omitted_examples': sum(v for v in (missing, null) if isinstance(v, int))}


def assert_components(keys, expected):
    assert keys['by_path'] == expected
    assert [item['path'] for item in expected] == keys['paths']
    for count in ['missing_fields', 'null_fields']:
        assert sum(item[count] for item in expected) == keys[count]


@pytest.mark.parametrize('example_limit', [0, 1, 5])
def test_nested_composite_breakdown_without_retained_key_examples(example_limit):
    declaration = catalog([['obj', 'id'], ['other']])
    rows = [{'obj': {'id': 1}, 'other': 2}, {'obj': {}, 'other': None},
            {'obj': None, 'other': 3}, {'obj': {'id': None}}]
    report = run([declaration, *map(ab_record, rows),
                  {'type': 'STATE', 'state': {'data': {}}}], example_limit=example_limit)
    users = report['streams'][0]
    assert_components(users['keys'], [component(['obj', 'id'], 2, 1), component(['other'], 1, 1)])
    assert users['keys']['records_with_missing_or_null_keys'] == 3
    assert users['records'] == 4 and users['errors_by_code'] == {'TC010': 5}
    assert len(users['examples']) == example_limit and users['omitted_examples'] == 5 - example_limit
    assert report['exit_code'] == 1 and report['coverage'] == 'complete_input'


@pytest.mark.parametrize('parent', [{}, None, 7, False, [], [{'id': 1}]])
def test_untraversable_parent_counts_as_missing_leaf(parent):
    report = run([catalog([['obj', 'id']]), ab_record({'obj': parent}),
                  {'type': 'STATE', 'state': {'data': {}}}], example_limit=0)
    assert_components(report['streams'][0]['keys'], [component(['obj', 'id'], missing=1)])
    assert report['errors_by_code'] == {'TC010': 1}


def test_absent_parent_and_paths_ending_at_parent_distinguish_null_from_missing():
    report = run([catalog([['obj'], ['obj', 'id']]), ab_record({}), ab_record({'obj': None}),
                  ab_record({'obj': {'id': None}}), {'type': 'STATE', 'state': {'data': {}}}],
                 example_limit=0)
    assert_components(report['streams'][0]['keys'], [component(['obj'], 1, 1),
                                                    component(['obj', 'id'], 2, 1)])
    assert report['errors_by_code'] == {'TC010': 5}


def test_sample_crowding_and_non_null_values_do_not_hide_other_components():
    declaration = singer(['z', 'a', 'never_failed'])
    declaration['schema']['required'] = ['required_field']
    report = run([declaration, record({'z': 0, 'a': False, 'never_failed': ''}),
                  record({'z': None, 'never_failed': 'VALUE_DO_NOT_COPY'}), STATE], example_limit=1)
    users = report['streams'][0]
    assert_components(users['keys'], [component(['z'], null=1), component(['a'], missing=1),
                                      component(['never_failed'])])
    assert users['examples'][0]['code'] == 'TC006'
    assert users['omitted_examples'] == 3 and users['keys']['records_with_missing_or_null_keys'] == 1
    assert 'VALUE_DO_NOT_COPY' not in json.dumps(report)


@pytest.mark.parametrize('keys', [[], ['z', 'a'], ['id', 'id']])
def test_zero_record_streams_include_declared_paths_in_order(keys):
    report = run([singer(keys), STATE])
    assert_components(report['streams'][0]['keys'], [component([key]) for key in keys])
    assert report['exit_code'] == 0


def test_repeated_paths_schemas_and_rejected_records_keep_occurrence_semantics():
    declaration = singer(['id', 'id'])
    report = run([declaration, record({}), declaration, singer(['replacement']), record(None),
                  record({'id': None}), record({'id': 1}), STATE], example_limit=0)
    users = report['streams'][0]
    assert_components(users['keys'], [component(['id'], 1, 1), component(['id'], 1, 1)])
    assert users['schemas'] == 2 and users['records'] == 3
    assert users['keys']['records_with_missing_or_null_keys'] == 2
    assert users['errors_by_code'] == {'TC003': 1, 'TC009': 1, 'TC010': 4}
    undeclared = record({})
    undeclared['stream'] = 'unknown'
    report = run([singer([]), undeclared, record({}), STATE])
    assert_components(report['streams'][0]['keys'], [])
    assert report['streams'][0]['records'] == 1 and report['unassigned']['errors_by_code'] == {'TC005': 1}


def test_schema_evaluation_failure_still_counts_components():
    declaration = singer(['id', 'other'])
    declaration['schema'] = {'$ref': '#/missing'}
    report = run([declaration, record({'id': None}), STATE], example_limit=0)
    assert_components(report['streams'][0]['keys'], [component(['id'], null=1), component(['other'], missing=1)])
    assert report['errors_by_code'] == {'TC004': 1}
    assert 'checks_after_schema_evaluation_failure' in report['not_evaluated']


def test_partial_operational_stop_excludes_unread_records():
    report = run([singer(['id', 'other']), record({}), record({'id': None}),
                  record({'id': 1, 'other': 1}), record({'id': 2, 'other': 2}), record({}), STATE],
                 check_duplicates=True, duplicate_limit=1, example_limit=0)
    users = report['streams'][0]
    assert_components(users['keys'], [component(['id'], 1, 1), component(['other'], missing=2)])
    assert users['records'] == 4 and users['keys']['records_with_missing_or_null_keys'] == 2
    assert report['exit_code'] == 2 and report['coverage'] == 'partial_input' and report['stopped_early']


def test_nested_paths_do_not_collide_with_literal_names_or_namespaces():
    paths = [['a.b'], ['a', 'b'], [''], ['é/雪']]
    first = catalog(paths, 'a')['catalog']['streams'][0]
    second = catalog(paths, 'b')['catalog']['streams'][0]
    # Configured keys take precedence over the discovery fallback.
    first = {'stream': first, 'primary_key': paths}
    first['stream']['source_defined_primary_key'] = [['ignored']]
    report = run([{'type': 'CATALOG', 'catalog': {'streams': [second, first]}},
                  ab_record({'a.b': None, 'a': {'b': 0}, '': False, 'é/雪': ''}, 'a'),
                  ab_record({'a.b': 1, 'a': None, '': None}, 'b'),
                  {'type': 'STATE', 'state': {'data': {}}}], example_limit=0)
    a, b = report['streams']
    assert a['namespace'] == 'a' and b['namespace'] == 'b'
    assert_components(a['keys'], [component(['a.b'], null=1), *map(component, paths[1:])])
    assert_components(b['keys'], [component(['a.b']), component(['a', 'b'], missing=1),
                                  component([''], null=1), component(['é/雪'], missing=1)])


def test_local_batch_and_inline_rows_share_component_counters(tmp_path):
    (tmp_path / 'rows.jsonl').write_text('{}\n{"id":null,"other":1}\n')
    batch = {'type': 'BATCH', 'stream': 'users', 'encoding': {'format': 'jsonl'}, 'manifest': ['rows.jsonl']}
    report = run([singer(['id', 'other']), batch, record({'id': 1, 'other': None}), STATE],
                 batch_root=tmp_path, example_limit=0)
    users = report['streams'][0]
    assert_components(users['keys'], [component(['id'], 1, 1), component(['other'], 1, 1)])
    assert users['records'] == users['keys']['records_with_missing_or_null_keys'] == 3
    assert report['coverage'] == 'complete_input'


def test_report_schema_requires_well_formed_nonnegative_component_counts():
    schema = json.loads(Path('docs/tapcheck/quality-report.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    report = run([singer(['id']), record({}), STATE])
    validator.validate(report)
    for broken in [None, {}, [{'path': [], 'missing_fields': 0, 'null_fields': 0}],
                   [component([1])], [component(['id'], missing=True)],
                   [component(['id'], missing=-1)], [component(['id'], null='1')],
                   [{'path': ['id'], 'missing_fields': 0}],
                   [{**component(['id']), 'value': 'DO_NOT_COPY'}]]:
        invalid = deepcopy(report)
        invalid['streams'][0]['keys']['by_path'] = broken
        with pytest.raises(ValidationError):
            validator.validate(invalid)
    invalid = deepcopy(report)
    del invalid['streams'][0]['keys']['by_path']
    with pytest.raises(ValidationError):
        validator.validate(invalid)
