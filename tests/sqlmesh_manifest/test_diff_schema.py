"""Public diff-v1 structural schema and its additive compatibility boundary."""
import json
from importlib.resources import files

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from jsonschema import Draft7Validator


def test_packaged_diff_schema():
    schema = json.loads(files('datawrench.sqlmesh_manifest').joinpath('diff-v1.json').read_text())
    Draft7Validator.check_schema(schema)
    assert schema['$schema'] == 'http://json-schema.org/draft-07/schema#'
    assert schema['properties']['diff_version'] == {'type': 'integer', 'const': 1}


KINDS = ('model_added', 'model_removed', 'dependencies_changed', 'column_added',
         'column_removed', 'column_changed', 'source_added', 'source_removed')


def column():
    return {'name': 'id', 'description': '', 'data_type': None, 'meta': {},
            'constraints': [], 'quote': None, 'tags': [], '_extra': {},
            'granularity': None, 'config': {'meta': {}, 'tags': [], '_extra': {}}}


def event(kind):
    c = {'kind': kind, 'node_id': 'source.example.raw.input' if kind.startswith('source_') else 'model.example.final',
         'before_file': None, 'after_file': None}
    if kind == 'dependencies_changed':
        c.update(added=[{'node_id': 'source.example.raw.input', 'file': None}], removed=[])
    if kind.startswith('column_'):
        c.update(column='id', before=None if kind == 'column_added' else column(),
                 after=None if kind == 'column_removed' else column())
    return c


def report(change=None):
    summary = dict.fromkeys(KINDS, 0)
    if change:
        summary[change['kind']] = 1
    return {'diff_version': 1, 'has_changes': change is not None,
            'summary': summary, 'changes': [] if change is None else [change]}


@pytest.mark.parametrize('kind', KINDS)
def test_known_event_null_provenance(diff_schema, kind):
    diff_schema.validate(report(event(kind)))


@pytest.mark.parametrize('field', ['diff_version', 'has_changes', 'summary', 'changes'])
def test_missing_envelope_fields(diff_schema, field):
    data = report()
    del data[field]
    assert not diff_schema.is_valid(data)


@pytest.mark.parametrize('data', [None, [], 'report', 1, True])
def test_nonobject_envelope(diff_schema, data):
    assert not diff_schema.is_valid(data)


@pytest.mark.parametrize('field,value', [
    ('diff_version', 0), ('diff_version', 2), ('diff_version', '1'), ('diff_version', True),
    ('diff_version', None), ('diff_version', 1.5),
    ('has_changes', 0), ('has_changes', 'false'), ('has_changes', None),
    ('summary', []), ('summary', None), ('changes', {}), ('changes', None),
    ('has_changes', True), ('changes', [event('model_added')]),
])
def test_bad_envelope_fields(diff_schema, field, value):
    data = report()
    data[field] = value
    assert not diff_schema.is_valid(data)


@pytest.mark.parametrize('kind', KINDS)
def test_all_current_counters_required(diff_schema, kind):
    data = report()
    del data['summary'][kind]
    assert not diff_schema.is_valid(data)


@pytest.mark.parametrize('value', [-1, 0.5, True, '0', None])
@pytest.mark.parametrize('key', ['source_added', 'future_kind'])
def test_bad_counter(diff_schema, key, value):
    data = report(event('source_added'))
    data['summary'][key] = value
    assert not diff_schema.is_valid(data)


@pytest.mark.parametrize('key', ['model_added', 'future_kind'])
def test_unchanged_requires_zero_counters(diff_schema, key):
    data = report()
    data['summary'][key] = 1
    assert not diff_schema.is_valid(data)


@pytest.mark.parametrize('kind', [*KINDS, 'future_kind'])
@pytest.mark.parametrize('field', ['kind', 'node_id', 'before_file', 'after_file'])
def test_every_kind_requires_base_fields(diff_schema, kind, field):
    change = event(kind)
    del change[field]
    assert not diff_schema.is_valid(report(change) if field != 'kind' else {
        **report(), 'has_changes': True, 'changes': [change]})


@pytest.mark.parametrize('field,value', [('kind', 1), ('node_id', None), ('node_id', []),
                                         ('before_file', 1), ('after_file', {})])
def test_bad_event_base_types(diff_schema, field, value):
    data = report(event('model_added'))
    data['changes'][0][field] = value
    assert not diff_schema.is_valid(data)


@pytest.mark.parametrize('kind', ['model_added', 'model_removed', 'source_added', 'source_removed'])
@pytest.mark.parametrize('field', ['column', 'before', 'after', 'added', 'removed'])
def test_lifecycle_rejects_payloads(diff_schema, kind, field):
    change = event(kind)
    change[field] = None
    assert not diff_schema.is_valid(report(change))


@pytest.mark.parametrize('kind', ['model_added', 'model_removed', 'source_added', 'source_removed'])
def test_lifecycle_absent_side_is_null(diff_schema, kind):
    change = event(kind)
    change['before_file' if kind.endswith('added') else 'after_file'] = 'wrong.sql'
    assert not diff_schema.is_valid(report(change))


@pytest.mark.parametrize('field,value', [
    ('added', None), ('removed', {}), ('added', []),
    ('added', [{}]), ('added', [{'node_id': 'model.example.upstream'}]),
    ('removed', [{'file': None}]), ('added', [{'node_id': 1, 'file': None}]),
    ('added', [{'node_id': 'source.example.raw.input', 'file': []}]),
    ('column', 'id'), ('before', None), ('after', {}),
])
def test_bad_dependency_payload(diff_schema, field, value):
    change = event('dependencies_changed')
    change[field] = value
    assert not diff_schema.is_valid(report(change))


@pytest.mark.parametrize('field', ['added', 'removed'])
def test_dependency_arrays_required(diff_schema, field):
    change = event('dependencies_changed')
    del change[field]
    assert not diff_schema.is_valid(report(change))


@pytest.mark.parametrize('kind', ['column_added', 'column_removed', 'column_changed'])
@pytest.mark.parametrize('field', ['column', 'before', 'after'])
def test_column_payload_fields_required(diff_schema, kind, field):
    change = event(kind)
    del change[field]
    assert not diff_schema.is_valid(report(change))


@pytest.mark.parametrize('kind,field,value', [
    ('column_added', 'before', column()), ('column_added', 'after', None),
    ('column_removed', 'before', None), ('column_removed', 'after', column()),
    ('column_changed', 'before', None), ('column_changed', 'after', None),
    ('column_changed', 'column', 1), ('column_changed', 'after', []),
    ('column_changed', 'added', []), ('column_changed', 'removed', []),
])
def test_bad_column_sides(diff_schema, kind, field, value):
    change = event(kind)
    change[field] = value
    assert not diff_schema.is_valid(report(change))


@pytest.mark.parametrize('field', list(column()))
def test_normalized_column_fields_required(diff_schema, field):
    change = event('column_changed')
    del change['after'][field]
    assert not diff_schema.is_valid(report(change))


@pytest.mark.parametrize('field,value', [
    ('name', None), ('description', 3), ('data_type', False), ('meta', []),
    ('_extra', None), ('quote', 'true'), ('granularity', 'fortnight'),
    ('tags', ['x', 'x']), ('tags', [None]), ('constraints', {}),
    ('constraints', [{}]), ('constraints', [{'type': 'unknown'}]),
    ('constraints', [{'type': 'check', 'expression': 1}]),
    ('constraints', [{'type': 'not_null', 'warn_unenforced': 1}]),
    ('constraints', [{'type': 'foreign_key', 'to_columns': [1]}]),
    ('doc_blocks', [False]), ('config', None), ('config', {}),
    ('config', {'meta': {}, '_extra': {}, 'tags': ['x', 'x']}),
    ('config', {'meta': [], '_extra': {}, 'tags': []}),
])
def test_bad_column_metadata(diff_schema, field, value):
    change = event('column_changed')
    change['after'][field] = value
    assert not diff_schema.is_valid(report(change))


def test_additive_compatibility_and_metadata(diff_schema, monkeypatch):
    import socket
    import requests
    from datawrench.sqlmesh_manifest import cli

    def blocked(*args, **kwargs):
        pytest.fail('Standalone schema validation must not load projects or access network')

    monkeypatch.setattr(cli, 'load_project', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    data = report(event('column_changed'))
    data['future_envelope'] = {'anything': [True, None, 1]}
    data['summary']['future_kind'] = 1
    data['changes'].append({**event('future_kind'), 'arbitrary_payload': [None, 1]})
    data['changes'][0]['future_field'] = 'extension'
    metadata = data['changes'][0]['after']
    metadata.update(future_column={'nested': [True, None, 3]}, doc_blocks=['doc.example'],
                    meta={'arbitrary': [True, {'a': None}, 1]},
                    config={'meta': {}, 'tags': ['a'], '_extra': {}, 'custom': False},
                    constraints=[{'type': 'check', 'expression': 'id > 0', 'custom': {}}])
    diff_schema.validate(data)
    # Integrally valued JSON numbers are integers under Draft 7, as documented.
    data['diff_version'] = 1.0
    data['summary']['model_added'] = 0.0
    diff_schema.validate(data)
    diff_schema.validate({**report(), 'future_field': [],
                          'summary': {**report()['summary'], 'future_kind': 0}})
    dependency = report(event('dependencies_changed'))
    dependency['changes'][0]['added'][0]['future_field'] = True
    diff_schema.validate(dependency)
